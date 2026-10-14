import httpx
import pytest

from github_analysis_agent import GitHubClient
from github_analysis_agent.errors import *


def client(handler):
    return GitHubClient(token="", transport=httpx.MockTransport(handler))


def test_request_encoding_ref_headers_timeout(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    def handler(request):
        assert request.url.path == "/repos/owner/repo/contents/目录/a b?#.txt"
        assert "%E7%9B%AE" in str(request.url)
        assert "%3F%23" in str(request.url)
        assert request.url.params["ref"] == "feature/a b"
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert request.extensions["timeout"]["read"] == 3
        return httpx.Response(200, json=[])
    with GitHubClient(timeout=3, transport=httpx.MockTransport(handler)) as c:
        assert c.get_contents("owner", "repo", "目录/a b?#.txt", "feature/a b") == []


def test_root_anonymous():
    def handler(request):
        assert request.url.path == "/repos/o/r/contents"
        assert not request.url.query
        assert "authorization" not in request.headers
        return httpx.Response(200, json=[])
    with client(handler) as c:
        assert c.get_contents("o", "r") == []


@pytest.mark.parametrize("path", ["../a", "a/../b", "/a", "a\\b", "a//b", "./a", "a/", "a\x00b"])
def test_invalid_paths_no_request(path):
    with client(lambda r: pytest.fail("Must not request")) as c:
        with pytest.raises(InvalidArgumentError):
            c.get_contents("o", "r", path)


@pytest.mark.parametrize("owner,repo,ref", [("o/x", "r", None), ("o", "..", None), ("o", "r", ""), ("o", "r", 4)])
def test_invalid_identifiers(owner, repo, ref):
    with client(lambda r: pytest.fail("Must not request")) as c:
        with pytest.raises(InvalidArgumentError):
            c.get_contents(owner, repo, ref=ref)


@pytest.mark.parametrize("status,message,headers,error,retryable", [
    (401, "bad", {}, AuthenticationError, False),
    (403, "forbidden", {}, PermissionDeniedError, False),
    (403, "API rate limit exceeded", {"x-ratelimit-remaining":"0"}, RateLimitError, True),
    (403, "secondary rate limit", {"retry-after":"2"}, RateLimitError, True),
    (429, "slow", {"retry-after":"5", "x-ratelimit-reset":"123"}, RateLimitError, True),
    (404, "Not Found", {}, NotFoundError, False),
    (404, "This repository is empty.", {}, EmptyRepositoryError, False),
    (500, "failed", {}, ServiceError, True),
    (503, "failed", {}, ServiceError, True),
    (302, "redirect", {"location":"https://example.com"}, GitHubError, False),
])
def test_http_errors(status, message, headers, error, retryable):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"message":message}, headers={**headers, "x-github-request-id":"req"})
    with client(handler) as c:
        with pytest.raises(error) as caught:
            c.get_contents("o", "r")
    assert len(calls) == 1
    assert caught.value.status_code == status
    assert caught.value.retryable is retryable
    assert caught.value.retry_after == headers.get("retry-after")
    assert caught.value.rate_limit_reset == headers.get("x-ratelimit-reset")
    assert caught.value.request_id == "req"


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
def test_network_failure(failure):
    def handler(request):
        raise failure("secret request detail", request=request)
    with client(handler) as c:
        with pytest.raises(NetworkError) as caught:
            c.get_contents("o", "r")
    assert caught.value.retryable
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("body", [b"not json", b"null", b'"string"'])
def test_bad_json(body):
    with client(lambda r: httpx.Response(200, content=body)) as c:
        with pytest.raises(ResponseFormatError):
            c.get_contents("o", "r")


def test_error_status_before_json():
    with client(lambda r: httpx.Response(502, content=b"bad gateway")) as c:
        with pytest.raises(ServiceError):
            c.get_contents("o", "r")


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "1"])
def test_invalid_timeout(timeout):
    with pytest.raises(InvalidArgumentError):
        GitHubClient(timeout=timeout)


def test_logs_do_not_expose_token(caplog):
    with caplog.at_level("INFO"):
        with GitHubClient(token="secret-token", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))) as c:
            c.get_contents("o", "r")
    assert "secret-token" not in caplog.text
