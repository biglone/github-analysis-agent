import base64
from copy import deepcopy
import json
import logging

import httpx
import pytest

from github_analysis_agent import GitHubClient
from github_analysis_agent.agent_runtime import AgentConfigurationError, AgentRuntime
from github_analysis_agent.model_adapter import ModelResponse, TokenUsage, ToolCall
from github_analysis_agent.model_errors import ModelAuthenticationError


def answer(text="done", *, prompt=2, completion=3):
    return ModelResponse(text, (), TokenUsage(prompt, completion, prompt + completion))


def tool(name, arguments, call_id="call-1"):
    return ToolCall(call_id, name, arguments)


class ScriptedModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, messages, *, timeout):
        self.requests.append({"messages": deepcopy(messages), "timeout": timeout})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(messages, timeout)
        return item


def file_response(text="README text"):
    raw = text.encode("utf-8")
    return {"name": "README.md", "path": "README.md", "type": "file",
            "sha": "sha-file", "size": len(raw), "encoding": "base64",
            "content": base64.b64encode(raw).decode("ascii")}


def directory_response():
    return [{"name": "README.md", "path": "README.md", "type": "file",
             "sha": "sha-file", "size": 11}]


def run_model(model, handler, **limits):
    github = GitHubClient(token="github-test-secret", transport=httpx.MockTransport(handler))
    runtime = AgentRuntime(model_adapter=model, github_client=github, **limits)
    return runtime, github


def tool_messages(messages):
    return [message for message in messages if message["role"] == "tool"]


def test_direct_model_answer_needs_no_github_request():
    model = ScriptedModel([answer("目录结构分析完成", prompt=5, completion=4)])
    runtime, github = run_model(model, lambda request: pytest.fail("No tool call expected"))
    try:
        result = runtime.run("分析结构", owner="owner", repo="repo")
    finally:
        runtime.close()
        github.close()
    assert result.answer == "目录结构分析完成"
    assert result.termination_reason == "completed"
    assert result.model_calls == 1
    assert result.tool_calls == 0
    assert result.token_usage.total_tokens == 9


def test_single_tool_call_result_is_returned_with_matching_id():
    model = ScriptedModel([
        ModelResponse(None, (tool("read_file", {"owner": "owner", "repo": "repo", "path": "README.md"}),), TokenUsage(3, 1, 4)),
        answer("README 说明了项目结构", prompt=7, completion=5),
    ])
    def handler(request):
        assert request.url.path.endswith("/contents/README.md")
        assert 0 < request.extensions["timeout"]["read"] <= 120
        return httpx.Response(200, json=file_response("项目结构"))
    runtime, github = run_model(model, handler)
    try:
        result = runtime.run("读取 README", owner="owner", repo="repo")
    finally:
        runtime.close()
        github.close()
    history = model.requests[1]["messages"]
    assert history[-2]["tool_calls"][0]["id"] == "call-1"
    tool_result = history[-1]
    assert tool_result["role"] == "tool"
    assert tool_result["tool_call_id"] == "call-1"
    assert json.loads(tool_result["content"]) == {
        "ok": True,
        "result": {"path": "README.md", "sha": "sha-file", "size": len("项目结构".encode()), "text": "项目结构"},
    }
    assert result.answer == "README 说明了项目结构"
    assert result.tool_stats["read_file"].succeeded == 1
    assert result.token_usage.total_tokens == 16


def test_multiturn_directory_then_read_file_preserves_context_and_reasoning():
    model = ScriptedModel([
        ModelResponse(None, (tool("list_directory", {"owner": "owner", "repo": "repo"}, "list-1"),),
                      TokenUsage(4, 2, 6), "thinking one"),
        ModelResponse(None, (tool("read_file", {"owner": "owner", "repo": "repo", "path": "README.md"}, "read-1"),),
                      TokenUsage(8, 3, 11), "thinking two"),
        answer("仓库包含 README.md", prompt=10, completion=4),
    ])
    requests = []
    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(200, json=directory_response() if request.url.path.endswith("/contents")
                              else file_response())
    runtime, github = run_model(model, handler)
    try:
        result = runtime.run("分析仓库结构", owner="owner", repo="repo")
    finally:
        runtime.close()
        github.close()
    assert requests == ["/repos/owner/repo/contents", "/repos/owner/repo/contents/README.md"]
    second_history = model.requests[1]["messages"]
    assert second_history[-2]["reasoning_content"] == "thinking one"
    assert tool_messages(second_history)[0]["tool_call_id"] == "list-1"
    third_history = model.requests[2]["messages"]
    assert third_history[-2]["reasoning_content"] == "thinking two"
    assert [message["tool_call_id"] for message in tool_messages(third_history)] == ["list-1", "read-1"]
    assert result.model_calls == 3
    assert result.tool_calls == 2
    assert result.token_usage.prompt_tokens == 22
    assert result.token_usage.completion_tokens == 9
    assert result.token_usage.total_tokens == 31


def test_multiple_calls_in_one_response_keep_result_order_and_ids():
    model = ScriptedModel([
        ModelResponse(None, (
            tool("list_directory", {"owner": "owner", "repo": "repo"}, "batch-list"),
            tool("read_file", {"owner": "owner", "repo": "repo", "path": "README.md"}, "batch-read"),
        ), TokenUsage(1, 2, 3)), answer("done"),
    ])
    runtime, github = run_model(model, lambda req: httpx.Response(
        200, json=directory_response() if req.url.path.endswith("/contents") else file_response()))
    try:
        result = runtime.run("Inspect", owner="owner", repo="repo")
    finally:
        runtime.close()
        github.close()
    messages = model.requests[1]["messages"]
    assert [m["id"] for m in messages[-3]["tool_calls"]] == ["batch-list", "batch-read"]
    assert [m["tool_call_id"] for m in messages[-2:]] == ["batch-list", "batch-read"]
    assert result.tool_calls == 2
    assert result.tool_stats["list_directory"].succeeded == 1
    assert result.tool_stats["read_file"].succeeded == 1


@pytest.mark.parametrize("tool_call,error_type", [
    (tool("run_shell", {"command": "whoami"}, "unknown-1"), "UnknownTool"),
    (tool("read_file", {"owner": "owner", "repo": "repo", "path": "README.md", "extra": True}, "extra-1"), "InvalidToolArguments"),
    (tool("read_file", {"owner": "other", "repo": "repo", "path": "README.md"}, "scope-1"), "UnauthorizedRepository"),
])
def test_unknown_invalid_and_out_of_scope_calls_are_rejected(tool_call, error_type):
    model = ScriptedModel([ModelResponse(None, (tool_call,), TokenUsage(None, None, None)), answer()])
    runtime, github = run_model(model, lambda req: pytest.fail("Rejected call must not access GitHub"))
    try:
        result = runtime.run("Read the repo", owner="owner", repo="repo")
    finally:
        runtime.close()
        github.close()
    returned = json.loads(model.requests[1]["messages"][-1]["content"])
    assert returned["error"]["type"] == error_type
    assert model.requests[1]["messages"][-1]["tool_call_id"] == tool_call.id
    assert result.termination_reason == "completed"


def test_not_found_is_safely_returned_then_model_can_adjust_path():
    model = ScriptedModel([
        ModelResponse(None, (tool("read_file", {"owner": "owner", "repo": "repo", "path": "wrong.md"}, "bad-path"),), TokenUsage(1, 1, 2)),
        ModelResponse(None, (tool("read_file", {"owner": "owner", "repo": "repo", "path": "README.md"}, "good-path"),), TokenUsage(1, 1, 2)),
        answer(),
    ])
    def handler(request):
        if request.url.path.endswith("wrong.md"):
            return httpx.Response(404, json={"message": "private sensitive response"})
        return httpx.Response(200, json=file_response())
    runtime, github = run_model(model, handler)
    try:
        result = runtime.run("Read the README", owner="owner", repo="repo")
    finally:
        runtime.close()
        github.close()
    error_result = json.loads(model.requests[1]["messages"][-1]["content"])
    assert error_result["error"]["type"] == "GitHubNotFoundError"
    assert error_result["error"]["recoverable"] is True
    assert "private sensitive response" not in model.requests[1]["messages"][-1]["content"]
    assert result.tool_calls == 2


def test_permission_error_stops_without_retrying_tools():
    model = ScriptedModel([ModelResponse(None, (tool("list_directory", {"owner": "owner", "repo": "repo"}),), TokenUsage(1, 1, 2))])
    runtime, github = run_model(model, lambda req: httpx.Response(403, json={"message": "secret permission detail"}))
    try:
        result = runtime.run("Inspect", owner="owner", repo="repo")
    finally:
        runtime.close()
        github.close()
    assert result.termination_reason == "github_auth_error"
    assert result.model_calls == 1
    assert result.error == "GitHub authentication or repository permission failed"
    assert "secret permission detail" not in result.error


def test_generic_tool_exception_is_sanitized_and_model_may_continue():
    model = ScriptedModel([ModelResponse(None, (tool("list_directory", {"owner": "owner", "repo": "repo"}),), TokenUsage(1, 1, 2)), answer()])
    def handler(request):
        raise RuntimeError("secret tool response detail")
    runtime, github = run_model(model, handler)
    try:
        result = runtime.run("Inspect", owner="owner", repo="repo")
    finally:
        runtime.close()
        github.close()
    content = model.requests[1]["messages"][-1]["content"]
    assert "secret tool response detail" not in content
    assert json.loads(content)["error"]["type"] == "ToolExecutionError"
    assert result.termination_reason == "completed"


def test_identical_failed_call_is_suppressed():
    model = ScriptedModel([
        ModelResponse(None, (tool("read_file", {"owner": "owner", "repo": "repo", "path": "bad.md"}, "first"),), TokenUsage(None, None, None)),
        ModelResponse(None, (tool("read_file", {"owner": "owner", "repo": "repo", "path": "bad.md"}, "again"),), TokenUsage(None, None, None)),
        answer(),
    ])
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(404, json={"message": "not found"})
    runtime, github = run_model(model, handler)
    try:
        result = runtime.run("Inspect", owner="owner", repo="repo")
    finally:
        runtime.close()
        github.close()
    assert len(calls) == 1
    last_tool_message = tool_messages(model.requests[2]["messages"])[-1]
    assert json.loads(last_tool_message["content"])["error"]["type"] == "RepeatedFailure"
    assert result.tool_calls == 2


def test_model_api_error_is_distinct_and_does_not_leak_details(caplog):
    model = ScriptedModel([ModelAuthenticationError("API key is github-test-secret")])
    runtime, github = run_model(model, lambda req: pytest.fail("No tool expected"))
    try:
        with caplog.at_level(logging.INFO):
            result = runtime.run("Inspect", owner="owner", repo="repo")
    finally:
        runtime.close()
        github.close()
    assert result.termination_reason == "model_error"
    assert result.error == "DeepSeek authentication failed"
    assert "github-test-secret" not in caplog.text


def test_max_turns_and_tool_call_limits_stop_the_loop():
    first_model = ScriptedModel([ModelResponse(None, (tool("list_directory", {"owner": "owner", "repo": "repo"}),), TokenUsage(1, 1, 2))])
    runtime, github = run_model(first_model, lambda req: httpx.Response(200, json=directory_response()), max_turns=1)
    try:
        turns_result = runtime.run("Inspect", owner="owner", repo="repo")
    finally:
        runtime.close()
        github.close()
    assert turns_result.termination_reason == "max_turns"
    assert turns_result.model_calls == 1

    second_model = ScriptedModel([
        ModelResponse(None, (tool("list_directory", {"owner": "owner", "repo": "repo"}, "one"),), TokenUsage(None, None, None)),
        ModelResponse(None, (tool("read_file", {"owner": "owner", "repo": "repo", "path": "README.md"}, "two"),), TokenUsage(None, None, None)),
    ])
    requests = []
    runtime, github = run_model(second_model, lambda req: (requests.append(req), httpx.Response(200, json=directory_response()))[1], max_tool_calls=1)
    try:
        calls_result = runtime.run("Inspect", owner="owner", repo="repo")
    finally:
        runtime.close()
        github.close()
    assert calls_result.termination_reason == "max_tool_calls"
    assert calls_result.model_calls == 2
    assert len(requests) == 1
    assert calls_result.tool_stats["read_file"].skipped == 1


def test_timeout_uses_remaining_budget_and_stops(monkeypatch):
    import github_analysis_agent.agent_runtime as runtime_module
    now = [10.0]
    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: now[0])
    model = ScriptedModel([lambda messages, timeout: (now.__setitem__(0, 12.0), answer())[1]])
    runtime, github = run_model(model, lambda req: pytest.fail("Timed out before tools"), timeout_seconds=1.0)
    try:
        result = runtime.run("Inspect", owner="owner", repo="repo")
    finally:
        runtime.close()
        github.close()
    assert model.requests[0]["timeout"] == 1.0
    assert result.termination_reason == "timeout"
    assert result.error == "Agent run timed out"


def test_tool_result_size_limit_returns_error_instead_of_large_content():
    model = ScriptedModel([
        ModelResponse(None, (tool("read_file", {"owner": "owner", "repo": "repo", "path": "README.md"}),), TokenUsage(None, None, None)),
        answer(),
    ])
    runtime, github = run_model(model, lambda req: httpx.Response(200, json=file_response("x" * 500)), max_tool_result_chars=256)
    try:
        result = runtime.run("Read it", owner="owner", repo="repo")
    finally:
        runtime.close()
        github.close()
    tool_result = json.loads(model.requests[1]["messages"][-1]["content"])
    assert tool_result["error"]["type"] == "ToolResultTooLarge"
    assert "x" * 90 not in model.requests[1]["messages"][-1]["content"]
    assert result.tool_stats["read_file"].failed == 1


def test_logs_have_run_stats_but_not_secrets_or_tool_content(caplog):
    secret_text = "repository-private-content"
    model = ScriptedModel([
        ModelResponse(None, (tool("read_file", {"owner": "owner", "repo": "repo", "path": "secret/path.txt"}),), TokenUsage(2, 1, 3)),
        answer("done"),
    ])
    runtime, github = run_model(model, lambda req: httpx.Response(200, json=file_response(secret_text)))
    try:
        with caplog.at_level(logging.INFO):
            result = runtime.run("Inspect", owner="owner", repo="repo")
    finally:
        runtime.close()
        github.close()
    assert "tool=read_file status=success" in caplog.text
    assert "agent finished reason=completed model_calls=2 tool_calls=1" in caplog.text
    assert "github-test-secret" not in caplog.text
    assert secret_text not in caplog.text
    assert "GitHub contents path=" not in caplog.text
    assert result.token_usage.total_tokens == 8


@pytest.mark.parametrize("limits", [
    {"max_turns": 0}, {"max_tool_calls": -1}, {"timeout_seconds": 0},
    {"max_tool_result_chars": True}, {"max_tool_result_chars": 255},
])
def test_invalid_runtime_limits(limits):
    with pytest.raises(AgentConfigurationError):
        AgentRuntime(model_adapter=ScriptedModel([]), github_client=object(), **limits)
