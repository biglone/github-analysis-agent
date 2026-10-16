"""Bounded, read-only Agent Loop joining DeepSeek and GitHub tools."""

from dataclasses import dataclass
import json
import logging
import math
import time

from .client import GitHubClient, validate_arguments
from .errors import (AuthenticationError, EmptyRepositoryError, GitHubError,
                     InvalidArgumentError, NetworkError, NotFoundError,
                     PermissionDeniedError, RateLimitError, ResponseFormatError,
                     ServiceError, UnsupportedContentError, WrongContentTypeError)
from .model_adapter import DeepSeekAdapter, TokenUsage
from .model_errors import (MissingAPIKeyError, ModelAdapterError,
                           ModelAuthenticationError, ModelNetworkError,
                           ModelRateLimitError, ModelServiceError,
                           ModelTimeoutError)
from .tools import list_directory, read_file

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolStats:
    requested: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class AgentResult:
    answer: str | None
    termination_reason: str
    model_calls: int
    tool_calls: int
    token_usage: TokenUsage
    elapsed_seconds: float
    tool_stats: dict[str, ToolStats]
    error: str | None = None


class AgentConfigurationError(ValueError):
    """Invalid runtime limits or task scope."""


class _ToolInputError(Exception):
    def __init__(self, message, *, error_type="InvalidToolArguments", fatal=False):
        super().__init__(message)
        self.error_type = error_type
        self.fatal = fatal


class AgentRuntime:
    """Run a bounded tool loop for one explicitly selected repository.

    Only ``list_directory`` and ``read_file`` are registered. Injected clients
    are caller-owned; internally-created clients are closed by ``close()``.
    """

    def __init__(self, *, model_adapter=None, github_client=None,
                 max_turns=8, max_tool_calls=12, timeout_seconds=120.0,
                 max_tool_result_chars=20_000):
        self._validate_positive_int("max_turns", max_turns)
        self._validate_positive_int("max_tool_calls", max_tool_calls)
        self._validate_positive_int("max_tool_result_chars", max_tool_result_chars)
        if max_tool_result_chars < 256:
            raise AgentConfigurationError("max_tool_result_chars must be at least 256 to fit safe error results")
        if (isinstance(timeout_seconds, bool) or
                not isinstance(timeout_seconds, (int, float)) or
                not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise AgentConfigurationError("timeout_seconds must be a positive finite number")
        self.max_turns = max_turns
        self.max_tool_calls = max_tool_calls
        self.timeout_seconds = float(timeout_seconds)
        self.max_tool_result_chars = max_tool_result_chars
        self._owns_model_adapter = model_adapter is None
        self.model_adapter = model_adapter if model_adapter is not None else DeepSeekAdapter(timeout=self.timeout_seconds)
        self._owns_github_client = github_client is None
        self.github_client = github_client if github_client is not None else GitHubClient(timeout=self.timeout_seconds)
        self._closed = False

    @staticmethod
    def _validate_positive_int(name, value):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise AgentConfigurationError(f"{name} must be a positive integer")

    def close(self):
        if not self._closed:
            if self._owns_github_client:
                self.github_client.close()
            if self._owns_model_adapter:
                self.model_adapter.close()
        self._closed = True

    def __enter__(self):
        if self._closed:
            raise AgentConfigurationError("AgentRuntime is already closed")
        return self

    def __exit__(self, *args):
        self.close()

    def run(self, task, *, owner, repo, ref=None):
        """Analyze one repository task and return the final answer and run stats."""
        if self._closed:
            raise AgentConfigurationError("AgentRuntime is closed")
        if not isinstance(task, str) or not task.strip():
            raise AgentConfigurationError("task must be a nonempty string")
        validate_arguments(owner, repo, "", ref)

        started = time.monotonic()
        deadline = started + self.timeout_seconds
        messages = [
            {"role": "system", "content": (
                "You are a read-only GitHub repository analysis assistant. "
                f"The only authorized repository is {owner}/{repo}. "
                "Use the provided read-only tools when needed. Do not claim "
                "you performed an operation unless its tool result confirms it."
            )},
            {"role": "user", "content": task.strip()},
        ]
        model_calls = 0
        requested_tool_calls = 0
        dispatched_tool_calls = 0
        token_totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        token_seen = set()
        stats = {}
        failed_calls = set()
        seen_call_ids = set()

        def finish(reason, answer=None, error=None):
            elapsed = max(0.0, time.monotonic() - started)
            usage = TokenUsage(**{
                field: token_totals[field] if field in token_seen else None
                for field in token_totals
            })
            result = AgentResult(
                answer=answer,
                termination_reason=reason,
                model_calls=model_calls,
                tool_calls=requested_tool_calls,
                token_usage=usage,
                elapsed_seconds=elapsed,
                tool_stats={name: ToolStats(**counts) for name, counts in stats.items()},
                error=error,
            )
            logger.info(
                "agent finished reason=%s model_calls=%d tool_calls=%d "
                "prompt_tokens=%s completion_tokens=%s total_tokens=%s elapsed_ms=%d",
                reason, model_calls, requested_tool_calls, usage.prompt_tokens,
                usage.completion_tokens, usage.total_tokens, int(elapsed * 1000),
            )
            return result

        for _ in range(self.max_turns):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return finish("timeout", error="Agent run timed out")

            model_calls += 1
            logger.info("agent model_call number=%d", model_calls)
            try:
                response = self.model_adapter.complete(messages, timeout=remaining)
            except ModelTimeoutError:
                return finish("timeout", error="DeepSeek request timed out")
            except ModelAdapterError as exc:
                safe_message = {
                    MissingAPIKeyError: "DEEPSEEK_API_KEY is not set",
                    ModelAuthenticationError: "DeepSeek authentication failed",
                    ModelRateLimitError: "DeepSeek rate limit exceeded",
                    ModelNetworkError: "Could not connect to DeepSeek",
                    ModelServiceError: "DeepSeek service request failed",
                }.get(type(exc), "DeepSeek model request failed")
                return finish("model_error", error=safe_message)
            except Exception as exc:
                logger.warning("agent model_call_failed error_type=%s", type(exc).__name__)
                return finish("model_error", error="DeepSeek request failed")

            usage = getattr(response, "usage", None)
            if usage is not None:
                for field in token_totals:
                    value = getattr(usage, field, None)
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        token_totals[field] += value
                        token_seen.add(field)
            if time.monotonic() >= deadline:
                return finish("timeout", error="Agent run timed out")

            calls = tuple(getattr(response, "tool_calls", ()) or ())
            if not calls:
                return finish("completed", answer=getattr(response, "text", None))

            invalid_id = any(
                not isinstance(getattr(call, "id", None), str) or
                not getattr(call, "id", "") or getattr(call, "id") in seen_call_ids
                for call in calls
            )
            invalid_call = any(
                not isinstance(getattr(call, "name", None), str) or
                not getattr(call, "name", "") or
                not isinstance(getattr(call, "arguments", None), dict)
                for call in calls
            )
            if invalid_id or invalid_call:
                return finish("invalid_model_response", error="Model returned an invalid tool call")
            seen_call_ids.update(call.id for call in calls)
            requested_tool_calls += len(calls)

            assistant = {"role": "assistant", "content": getattr(response, "text", None)}
            reasoning = getattr(response, "reasoning_content", None)
            if reasoning is not None:
                assistant["reasoning_content"] = reasoning
            assistant["tool_calls"] = [
                {"id": call.id, "type": "function", "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False, separators=(",", ":")),
                }}
                for call in calls
            ]
            messages.append(assistant)

            fatal_reason = None
            for index, call in enumerate(calls):
                name_key = call.name if call.name in ("list_directory", "read_file") else "unknown_tool"
                counts = stats.setdefault(name_key, {"requested": 0, "succeeded": 0, "failed": 0, "skipped": 0})
                counts["requested"] += 1
                if dispatched_tool_calls >= self.max_tool_calls:
                    counts["skipped"] += 1
                    result_data = self._error_result("ToolLimitReached", "Maximum tool call limit reached", recoverable=False)
                    fatal_reason = "max_tool_calls"
                    messages.append(self._tool_message(call.id, result_data))
                    for skipped in calls[index + 1:]:
                        skipped_key = skipped.name if skipped.name in ("list_directory", "read_file") else "unknown_tool"
                        skipped_counts = stats.setdefault(skipped_key, {"requested": 0, "succeeded": 0, "failed": 0, "skipped": 0})
                        skipped_counts["requested"] += 1
                        skipped_counts["skipped"] += 1
                        messages.append(self._tool_message(
                            skipped.id,
                            self._error_result("ToolLimitReached", "Maximum tool call limit reached", recoverable=False),
                        ))
                    break

                dispatched_tool_calls += 1
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    counts["skipped"] += 1
                    messages.append(self._tool_message(call.id, self._error_result("Timeout", "Agent run timed out", recoverable=False)))
                    fatal_reason = "timeout"
                    for skipped in calls[index + 1:]:
                        skipped_key = skipped.name if skipped.name in ("list_directory", "read_file") else "unknown_tool"
                        skipped_counts = stats.setdefault(skipped_key, {"requested": 0, "succeeded": 0, "failed": 0, "skipped": 0})
                        skipped_counts["requested"] += 1
                        skipped_counts["skipped"] += 1
                        messages.append(self._tool_message(skipped.id, self._error_result("Timeout", "Agent run timed out", recoverable=False)))
                    break

                call_key = self._failure_key(call)
                if call_key in failed_calls:
                    data = self._error_result(
                        "RepeatedFailure", "This exact tool call already failed; choose different arguments.", recoverable=True)
                    counts["failed"] += 1
                    self._log_tool(name_key, "suppressed", 0)
                else:
                    tool_started = time.monotonic()
                    try:
                        raw_result = self._execute(call, owner, repo, ref, remaining)
                        data = {"ok": True, "result": raw_result}
                        serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
                        if len(serialized) > self.max_tool_result_chars:
                            raise _ToolInputError(
                                "Tool result exceeds the configured size limit; request a narrower result.",
                                error_type="ToolResultTooLarge",
                            )
                        counts["succeeded"] += 1
                        self._log_tool(name_key, "success", int((time.monotonic() - tool_started) * 1000))
                    except _ToolInputError as exc:
                        data = self._error_result(exc.error_type, str(exc), recoverable=not exc.fatal)
                        counts["failed"] += 1
                        failed_calls.add(call_key)
                        self._log_tool(name_key, "rejected", int((time.monotonic() - tool_started) * 1000))
                        if exc.fatal:
                            fatal_reason = "tool_authorization_error"
                    except GitHubError as exc:
                        fatal, error_type, message, recoverable = self._safe_github_error(exc)
                        data = self._error_result(error_type, message, recoverable=recoverable)
                        counts["failed"] += 1
                        failed_calls.add(call_key)
                        self._log_tool(name_key, "error", int((time.monotonic() - tool_started) * 1000))
                        if fatal:
                            fatal_reason = "github_auth_error" if isinstance(exc, (AuthenticationError, PermissionDeniedError)) else "github_api_error"
                    except Exception as exc:
                        data = self._error_result("ToolExecutionError", "Tool execution failed; details were withheld.", recoverable=True)
                        counts["failed"] += 1
                        failed_calls.add(call_key)
                        logger.warning("agent tool_failed tool=%s error_type=%s", name_key, type(exc).__name__)
                messages.append(self._tool_message(call.id, data))

                if fatal_reason:
                    for skipped in calls[index + 1:]:
                        skipped_key = skipped.name if skipped.name in ("list_directory", "read_file") else "unknown_tool"
                        skipped_counts = stats.setdefault(skipped_key, {"requested": 0, "succeeded": 0, "failed": 0, "skipped": 0})
                        skipped_counts["requested"] += 1
                        skipped_counts["skipped"] += 1
                        messages.append(self._tool_message(
                            skipped.id,
                            self._error_result("NotExecuted", "Run stopped before this tool call could execute.", recoverable=False),
                        ))
                    break
                if time.monotonic() >= deadline:
                    fatal_reason = "timeout"
                    for skipped in calls[index + 1:]:
                        skipped_key = skipped.name if skipped.name in ("list_directory", "read_file") else "unknown_tool"
                        skipped_counts = stats.setdefault(skipped_key, {"requested": 0, "succeeded": 0, "failed": 0, "skipped": 0})
                        skipped_counts["requested"] += 1
                        skipped_counts["skipped"] += 1
                        messages.append(self._tool_message(
                            skipped.id, self._error_result("Timeout", "Agent run timed out", recoverable=False)))
                    break

            if fatal_reason:
                error = {
                    "timeout": "Agent run timed out",
                    "max_tool_calls": "Maximum tool call limit reached",
                    "github_auth_error": "GitHub authentication or repository permission failed",
                    "github_api_error": "GitHub request cannot continue",
                    "tool_authorization_error": "A tool requested access outside the authorized repository",
                }.get(fatal_reason, "Agent stopped")
                return finish(fatal_reason, error=error)

        return finish("max_turns", error="Maximum model interaction count reached")

    def _execute(self, call, owner, repo, ref, timeout):
        if call.name not in ("list_directory", "read_file"):
            raise _ToolInputError("Tool is not registered", error_type="UnknownTool")
        args = call.arguments
        if not isinstance(args, dict):
            raise _ToolInputError("Tool arguments must be a JSON object")
        required = {"owner", "repo"} if call.name == "list_directory" else {"owner", "repo", "path"}
        allowed = required | {"path", "ref"}
        if required - args.keys() or args.keys() - allowed:
            raise _ToolInputError("Tool arguments do not match the registered schema")
        if any(not isinstance(args.get(key), str) for key in required):
            raise _ToolInputError("Required tool arguments must be strings")
        if args["owner"] != owner or args["repo"] != repo:
            raise _ToolInputError("Requested repository is outside the authorized scope",
                                  error_type="UnauthorizedRepository")
        path = args.get("path", "")
        if not isinstance(path, str):
            raise _ToolInputError("path must be a string")
        requested_ref = args.get("ref")
        tool_ref = requested_ref if requested_ref is not None else ref
        if tool_ref is not None and not isinstance(tool_ref, str):
            raise _ToolInputError("ref must be a string or null")
        if ref is not None and tool_ref != ref:
            raise _ToolInputError("Requested ref is outside the authorized scope",
                                  error_type="UnauthorizedRef")
        if call.name == "list_directory":
            return list_directory(self.github_client, owner, repo, path, tool_ref, timeout=timeout)
        return read_file(self.github_client, owner, repo, path, tool_ref, timeout=timeout)

    @staticmethod
    def _tool_message(call_id, result):
        return {"role": "tool", "tool_call_id": call_id,
                "content": json.dumps(result, ensure_ascii=False, separators=(",", ":"))}

    @staticmethod
    def _error_result(error_type, message, *, recoverable):
        return {"ok": False, "error": {
            "type": error_type, "message": message, "recoverable": recoverable,
        }}

    @staticmethod
    def _safe_github_error(exc):
        if isinstance(exc, AuthenticationError):
            return True, "GitHubAuthenticationError", "GitHub authentication failed", False
        if isinstance(exc, PermissionDeniedError):
            return True, "GitHubPermissionError", "GitHub denied access to the repository", False
        if isinstance(exc, RateLimitError):
            return True, "GitHubRateLimitError", "GitHub rate limit reached", False
        if isinstance(exc, NotFoundError):
            return False, "GitHubNotFoundError", "Repository path or ref was not found or is inaccessible", True
        if isinstance(exc, InvalidArgumentError):
            return False, "GitHubInvalidArgument", "GitHub tool arguments are invalid", True
        if isinstance(exc, (WrongContentTypeError, UnsupportedContentError, ResponseFormatError, EmptyRepositoryError)):
            return False, type(exc).__name__, "GitHub returned content this tool cannot read", True
        if isinstance(exc, (NetworkError, ServiceError)):
            return False, type(exc).__name__, "GitHub request failed due to a network or service error", True
        return False, "GitHubError", "GitHub tool execution failed", True

    @staticmethod
    def _failure_key(call):
        try:
            args = json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            args = "<invalid>"
        return call.name, args

    @staticmethod
    def _log_tool(name, status, duration_ms):
        logger.info("agent tool_call tool=%s status=%s duration_ms=%d", name, status, duration_ms)
