import json
import os

import httpx
import pytest
from openai import OpenAI

from github_analysis_agent.model_adapter import DeepSeekAdapter, DEFAULT_MODEL
from github_analysis_agent.model_errors import (
    InvalidToolArgumentsError,
    MissingAPIKeyError,
    ModelAuthenticationError,
    ModelNetworkError,
    ModelRateLimitError,
    ModelServiceError,
    ModelTimeoutError,
)


def sdk_client(handler):
    return OpenAI(
        api_key="unit-test-secret",
        base_url="https://api.deepseek.com",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=0,
    )


def completion(*, content="hello", tool_calls=None, usage=None):
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"id": "chatcmpl-test", "object": "chat.completion", "created": 1,
            "model": DEFAULT_MODEL, "choices": [{"index": 0, "message": message,
            "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": usage or {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11}}


def test_text_response_and_default_model(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-secret")
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    def handler(request):
        payload = json.loads(request.content)
        assert payload["model"] == "deepseek-flash"
        assert len(payload["tools"]) == 2
        assert {item["function"]["name"] for item in payload["tools"]} == {"list_directory", "read_file"}
        return httpx.Response(200, json=completion(content="仓库内容"))
    adapter = DeepSeekAdapter(client=sdk_client(handler))
    result = adapter.complete([{"role": "user", "content": "hi"}])
    assert result.text == "仓库内容"
    assert result.tool_calls == ()
    assert (result.usage.prompt_tokens, result.usage.completion_tokens, result.usage.total_tokens) == (8, 3, 11)


def test_environment_model_and_text_without_tools(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-secret")
    monkeypatch.setenv("DEEPSEEK_MODEL", "custom-model")
    def handler(request):
        payload = json.loads(request.content)
        assert payload["model"] == "custom-model"
        assert "tools" not in payload
        return httpx.Response(200, json=completion())
    assert DeepSeekAdapter(client=sdk_client(handler)).complete([], enable_tools=False).text == "hello"


def test_preserves_reasoning_content_and_accepts_per_request_timeout(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-secret")
    body = completion()
    body["choices"][0]["message"]["reasoning_content"] = "private reasoning payload"
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=body)
    adapter = DeepSeekAdapter(client=sdk_client(handler))
    result = adapter.complete([], timeout=2.5)
    assert result.reasoning_content == "private reasoning payload"
    assert requests[0].extensions["timeout"]["read"] == 2.5
    history = [{"role": "user", "content": "question"}, {
        "role": "assistant", "content": None,
        "reasoning_content": result.reasoning_content,
        "tool_calls": [{"id": "call-1", "type": "function", "function": {
            "name": "read_file", "arguments": '{"owner":"o","repo":"r","path":"README.md"}'}}],
    }, {"role": "tool", "tool_call_id": "call-1", "content": '{"ok":true}'}]
    adapter.complete(history, timeout=1.5)
    sent_history = json.loads(requests[1].content)["messages"]
    assert sent_history[1]["reasoning_content"] == "private reasoning payload"
    adapter.close()


def test_single_tool_call_is_parsed_without_execution(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-secret")
    calls = [{"id": "call-1", "type": "function", "function": {
        "name": "read_file", "arguments": '{"owner":"biglone","repo":"github-analysis-agent","path":"README.md"}'}}]
    adapter = DeepSeekAdapter(client=sdk_client(lambda r: httpx.Response(200, json=completion(content=None, tool_calls=calls))))
    result = adapter.complete([{"role": "user", "content": "read readme"}])
    assert result.text is None
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "read_file"
    assert result.tool_calls[0].arguments["path"] == "README.md"
    assert result.tool_calls[0].id == "call-1"


def test_multiple_tool_calls_and_missing_usage(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-secret")
    calls = [
        {"id": "call-1", "type": "function", "function": {"name": "list_directory", "arguments": '{"owner":"o","repo":"r"}'}},
        {"id": "call-2", "type": "function", "function": {"name": "read_file", "arguments": '{"owner":"o","repo":"r","path":"a.txt"}'}},
    ]
    body = completion(content=None, tool_calls=calls)
    body["usage"] = {}
    result = DeepSeekAdapter(client=sdk_client(lambda r: httpx.Response(200, json=body))).complete([])
    assert [call.name for call in result.tool_calls] == ["list_directory", "read_file"]
    assert result.usage.total_tokens is None


@pytest.mark.parametrize("arguments", ["{bad json", "[]", "null"])
def test_invalid_tool_arguments_raise_safe_error(monkeypatch, arguments):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-secret")
    call = [{"id": "call", "type": "function", "function": {"name": "read_file", "arguments": arguments}}]
    adapter = DeepSeekAdapter(client=sdk_client(lambda r: httpx.Response(200, json=completion(content=None, tool_calls=call))))
    with pytest.raises(InvalidToolArgumentsError, match="invalid JSON|JSON object"):
        adapter.complete([])


def test_missing_key_is_clear(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(MissingAPIKeyError, match="DEEPSEEK_API_KEY"):
        DeepSeekAdapter()


def test_loads_key_and_model_from_project_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    (tmp_path / ".env").write_text(
        "DEEPSEEK_API_KEY=dotenv-test-key\nDEEPSEEK_MODEL=dotenv-model\n",
        encoding="utf-8",
    )
    adapter = DeepSeekAdapter(client=sdk_client(lambda r: httpx.Response(200, json=completion())))
    assert adapter.model == "dotenv-model"
    assert os.environ["DEEPSEEK_API_KEY"] == "dotenv-test-key"
    assert os.environ["DEEPSEEK_MODEL"] == "dotenv-model"
    # Keep the test key from leaking into the rest of the test process.
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    monkeypatch.delenv("DEEPSEEK_MODEL")


def test_process_environment_overrides_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "DEEPSEEK_API_KEY=dotenv-key\nDEEPSEEK_MODEL=dotenv-model\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "process-key")
    monkeypatch.setenv("DEEPSEEK_MODEL", "process-model")
    adapter = DeepSeekAdapter(client=sdk_client(lambda r: httpx.Response(200, json=completion())))
    assert adapter.model == "process-model"
    assert os.environ["DEEPSEEK_API_KEY"] == "process-key"


@pytest.mark.parametrize("status,error", [(401, ModelAuthenticationError), (429, ModelRateLimitError)])
def test_auth_and_rate_limit_errors_hide_key(monkeypatch, status, error):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-secret")
    adapter = DeepSeekAdapter(client=sdk_client(lambda r: httpx.Response(status, json={"error": {"message": "secret response text"}})))
    with pytest.raises(error) as caught:
        adapter.complete([])
    assert "unit-test-secret" not in str(caught.value)
    assert "secret response text" not in str(caught.value)


def test_server_error_is_retryable_and_safe(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-secret")
    adapter = DeepSeekAdapter(client=sdk_client(lambda r: httpx.Response(503, json={"error": {"message": "unit-test-secret"}})))
    with pytest.raises(ModelServiceError) as caught:
        adapter.complete([])
    assert caught.value.retryable
    assert "unit-test-secret" not in str(caught.value)


@pytest.mark.parametrize("failure,error", [
    (httpx.ReadTimeout("secret timeout"), ModelTimeoutError),
    (httpx.ConnectError("secret connection"), ModelNetworkError),
])
def test_timeout_and_network_errors_hide_details(monkeypatch, failure, error):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-secret")
    def handler(request):
        raise failure
    adapter = DeepSeekAdapter(client=sdk_client(handler))
    with pytest.raises(error) as caught:
        adapter.complete([])
    assert "unit-test-secret" not in str(caught.value)
    assert "secret" not in str(caught.value)
