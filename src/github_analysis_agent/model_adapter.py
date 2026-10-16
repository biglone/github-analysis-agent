"""DeepSeek Chat Completions adapter. It returns tool calls but never runs them."""

from dataclasses import dataclass
import json
import os

import httpx
from openai import (APIConnectionError, APIStatusError, APITimeoutError,
                    AuthenticationError, OpenAI, RateLimitError)

from .config import load_project_env
from .model_errors import (InvalidToolArgumentsError, MissingAPIKeyError,
                           ModelAuthenticationError, ModelNetworkError,
                           ModelRateLimitError, ModelResponseError,
                           ModelServiceError, ModelTimeoutError)

DEFAULT_MODEL = "deepseek-flash"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List the immediate entries in a GitHub repository directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner."},
                    "repo": {"type": "string", "description": "Repository name."},
                    "path": {"type": "string", "description": "Repository-relative directory path; empty for the root."},
                    "ref": {"type": ["string", "null"], "description": "Optional branch, tag, or commit."},
                },
                "required": ["owner", "repo"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file from a GitHub repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner."},
                    "repo": {"type": "string", "description": "Repository name."},
                    "path": {"type": "string", "description": "Repository-relative file path."},
                    "ref": {"type": ["string", "null"], "description": "Optional branch, tag, or commit."},
                },
                "required": ["owner", "repo", "path"],
                "additionalProperties": False,
            },
        },
    },
]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


@dataclass(frozen=True)
class ModelResponse:
    text: str | None
    tool_calls: tuple[ToolCall, ...]
    usage: TokenUsage
    reasoning_content: str | None = None


def _value(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class DeepSeekAdapter:
    """Call DeepSeek using the OpenAI SDK and normalize its response."""

    def __init__(self, *, timeout=30.0, client=None):
        load_project_env()
        api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            raise MissingAPIKeyError("DEEPSEEK_API_KEY is not set")
        self.model = os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self._client = client or OpenAI(
            api_key=api_key,
            base_url=DEEPSEEK_BASE_URL,
            timeout=timeout,
            max_retries=0,
        )

    def close(self):
        """Release the underlying OpenAI-compatible HTTP client."""
        self._client.close()

    def complete(self, messages, *, enable_tools=True, timeout=None):
        """Request a response; returned tool calls are data and are never executed."""
        request = {"model": self.model, "messages": messages}
        if enable_tools:
            request["tools"] = TOOL_DEFINITIONS
            request["tool_choice"] = "auto"
        try:
            client = self._client if timeout is None else self._client.with_options(timeout=timeout)
            response = client.chat.completions.create(**request)
        except APITimeoutError:
            raise ModelTimeoutError("DeepSeek request timed out", retryable=True) from None
        except AuthenticationError:
            raise ModelAuthenticationError("DeepSeek authentication failed", status_code=401) from None
        except RateLimitError:
            raise ModelRateLimitError("DeepSeek rate limit exceeded", retryable=True, status_code=429) from None
        except APIConnectionError:
            raise ModelNetworkError("Could not connect to DeepSeek", retryable=True) from None
        except APIStatusError as exc:
            status = exc.status_code
            if status == 401:
                raise ModelAuthenticationError("DeepSeek authentication failed", status_code=status) from None
            if status == 429:
                raise ModelRateLimitError("DeepSeek rate limit exceeded", retryable=True, status_code=status) from None
            if status >= 500:
                raise ModelServiceError("DeepSeek service error", retryable=True, status_code=status) from None
            raise ModelServiceError("DeepSeek rejected the request", status_code=status) from None
        except httpx.TimeoutException:
            raise ModelTimeoutError("DeepSeek request timed out", retryable=True) from None
        except httpx.HTTPError:
            raise ModelNetworkError("Could not connect to DeepSeek", retryable=True) from None

        choices = _value(response, "choices")
        if not choices:
            raise ModelResponseError("DeepSeek response did not contain a choice")
        message = _value(choices[0], "message")
        if message is None:
            raise ModelResponseError("DeepSeek response did not contain a message")
        raw_calls = _value(message, "tool_calls") or []
        calls = []
        for raw_call in raw_calls:
            function = _value(raw_call, "function")
            name = _value(function, "name")
            raw_arguments = _value(function, "arguments")
            if not isinstance(name, str) or not isinstance(raw_arguments, str):
                raise ModelResponseError("DeepSeek returned an invalid tool call")
            try:
                arguments = json.loads(raw_arguments)
            except (json.JSONDecodeError, TypeError):
                raise InvalidToolArgumentsError("DeepSeek returned invalid JSON tool arguments") from None
            if not isinstance(arguments, dict):
                raise InvalidToolArgumentsError("Tool arguments must be a JSON object")
            calls.append(ToolCall(id=_value(raw_call, "id", ""), name=name, arguments=arguments))
        usage = _value(response, "usage")
        token_usage = TokenUsage(
            prompt_tokens=_value(usage, "prompt_tokens"),
            completion_tokens=_value(usage, "completion_tokens"),
            total_tokens=_value(usage, "total_tokens"),
        )
        return ModelResponse(
            text=_value(message, "content"),
            tool_calls=tuple(calls),
            usage=token_usage,
            reasoning_content=_value(message, "reasoning_content"),
        )
