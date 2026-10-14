import base64
import httpx
import pytest

from github_analysis_agent import GitHubClient, list_directory, read_file
from github_analysis_agent.errors import *
from github_analysis_agent.tools import DirectoryLimitWarning, MAX_FILE_SIZE


def entry(raw=b"hello", **updates):
    return {"name":"a.txt", "path":"a.txt", "type":"file", "sha":"abc", "size":len(raw),
            "encoding":"base64", "content":base64.b64encode(raw).decode(), **updates}


def call(payload, tool=read_file, path="a.txt"):
    with GitHubClient(token="", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as c:
        return tool(c, "o", "r", path)


@pytest.mark.parametrize("path", ["", "src"])
def test_directory(path):
    data = [entry(), entry(type="dir", name="src", path="src", size=0)]
    result = call(data, list_directory, path)
    assert result == [{k:d[k] for k in ("name", "path", "type", "size", "sha")} for d in data]


def test_empty_directory():
    assert call([], list_directory, "") == []


def test_directory_limit():
    with pytest.warns(DirectoryLimitWarning, match="incomplete"):
        assert len(call([entry()] * 1000, list_directory, "")) == 1000


@pytest.mark.parametrize("special", [{"type":"symlink"}, {"type":"submodule"}, {"submodule_git_url":"https://example.com"}, {"target":"elsewhere"}])
def test_special_entries(special):
    data = entry(**special)
    assert call([data], list_directory, "")[0]["type"] in ("symlink", "submodule")
    with pytest.raises(UnsupportedContentError):
        call(data)


@pytest.mark.parametrize("raw", [b"hello", "中文内容\n".encode(), b"", b"x" * MAX_FILE_SIZE])
def test_utf8_file(raw):
    result = call(entry(raw))
    assert result == {"path":"a.txt", "sha":"abc", "size":len(raw), "text":raw.decode()}


def test_base64_newlines():
    data = entry("你好 world".encode())
    data["content"] = data["content"][:4] + "\r\n" + data["content"][4:] + "\n"
    assert call(data)["text"] == "你好 world"


@pytest.mark.parametrize("content", ["!!!", "a", "é", "aGVs bG8="])
def test_bad_base64(content):
    with pytest.raises(ContentDecodeError):
        call(entry(content=content))


@pytest.mark.parametrize("raw", [b"\xff", b"a\x00b"])
def test_binary(raw):
    with pytest.raises(UnsupportedContentError):
        call(entry(raw))


def test_large_file():
    with pytest.raises(UnsupportedContentError):
        call(entry(size=MAX_FILE_SIZE + 1))


def test_large_encoded_payload():
    with pytest.raises(UnsupportedContentError):
        call(entry(content="A" * (4 * ((MAX_FILE_SIZE+2)//3) + 4)))


def test_size_mismatch():
    with pytest.raises(ResponseFormatError):
        call(entry(size=0))


@pytest.mark.parametrize("encoding", ["none", "utf-8"])
def test_unsupported_encoding(encoding):
    with pytest.raises(UnsupportedContentError):
        call(entry(encoding=encoding))


@pytest.mark.parametrize("field", ["name", "path", "type", "size", "sha", "encoding", "content"])
def test_missing_fields(field):
    data = entry()
    del data[field]
    with pytest.raises(ResponseFormatError):
        call(data)


@pytest.mark.parametrize("updates", [{"size":True}, {"size":-1}, {"sha":None}, {"content":None}])
def test_invalid_fields(updates):
    with pytest.raises(ResponseFormatError):
        call(entry(**updates))


def test_wrong_type():
    with pytest.raises(WrongContentTypeError):
        call(entry(), list_directory)
    with pytest.raises(WrongContentTypeError):
        call([])
    with pytest.raises(WrongContentTypeError):
        call(entry(type="dir"))


def test_unknown_type():
    with pytest.raises(UnsupportedContentError):
        call(entry(type="unknown"))


def test_bad_directory_entry():
    with pytest.raises(ResponseFormatError):
        call([None], list_directory)


def test_empty_file_path_no_request():
    with GitHubClient(token="", transport=httpx.MockTransport(lambda r: pytest.fail("Must not request"))) as c:
        with pytest.raises(InvalidArgumentError):
            read_file(c, "o", "r", "")
