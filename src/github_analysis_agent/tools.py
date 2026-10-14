"""JSON-friendly repository tools. Base64 bytes and UTF-8 text are separate steps."""
import base64
import binascii
import warnings

from .client import validate_arguments
from .errors import (ContentDecodeError, InvalidArgumentError, ResponseFormatError,
                     UnsupportedContentError, WrongContentTypeError)

MAX_FILE_SIZE = 1_000_000


class DirectoryLimitWarning(UserWarning):
    """Contents API results may be incomplete at the 1,000-item limit."""


def _entry(data):
    if not isinstance(data, dict):
        raise ResponseFormatError("Expected a content entry object")
    for field in ("name", "path", "type", "sha"):
        if not isinstance(data.get(field), str) or not data[field]:
            raise ResponseFormatError(f"Missing or invalid {field}")
    size = data.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ResponseFormatError("Missing or invalid size")
    if data["type"] not in ("file", "dir", "symlink", "submodule"):
        raise UnsupportedContentError("Unsupported repository entry type")
    return {key: data[key] for key in ("name", "path", "type", "size", "sha")}


def list_directory(client, owner, repo, path="", ref=None):
    """List immediate children; emit DirectoryLimitWarning at 1,000 entries."""
    validate_arguments(owner, repo, path, ref)
    data = client.get_contents(owner, repo, path, ref)
    if not isinstance(data, list):
        if isinstance(data, dict):
            raise WrongContentTypeError("Expected a directory, received a content object")
        raise ResponseFormatError("Expected a directory array")
    result = [_entry(item) for item in data]
    # GitHub may label submodules as files; normalize explicit markers.
    for item, original in zip(result, data):
        if original.get("submodule_git_url") is not None:
            item["type"] = "submodule"
        elif "target" in original:
            item["type"] = "symlink"
    if len(result) >= 1000:
        warnings.warn("Directory has at least 1,000 entries; results may be incomplete",
                      DirectoryLimitWarning, stacklevel=2)
    return result


def read_file(client, owner, repo, path, ref=None):
    """Read a regular UTF-8 file of at most MAX_FILE_SIZE bytes."""
    validate_arguments(owner, repo, path, ref)
    if not path:
        raise InvalidArgumentError("read_file requires a nonempty file path")
    data = client.get_contents(owner, repo, path, ref)
    if isinstance(data, list):
        raise WrongContentTypeError("Expected a file, received a directory")
    entry = _entry(data)
    if entry["type"] == "dir":
        raise WrongContentTypeError("Expected a file, received a directory")
    if entry["type"] != "file" or "target" in data or data.get("submodule_git_url") is not None:
        raise UnsupportedContentError("Symlinks and submodules are not supported")
    if entry["size"] > MAX_FILE_SIZE:
        raise UnsupportedContentError("File exceeds 1,000,000 bytes")
    if "encoding" not in data or "content" not in data:
        raise ResponseFormatError("Missing file encoding or content")
    if data["encoding"] != "base64":
        raise UnsupportedContentError("Only inline Base64 content is supported")
    if not isinstance(data["content"], str):
        raise ResponseFormatError("File content must be a string")
    encoded = data["content"].replace("\n", "").replace("\r", "")
    if len(encoded) > 4 * ((MAX_FILE_SIZE + 2) // 3):
        raise UnsupportedContentError("Encoded file exceeds the size limit")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ContentDecodeError("Invalid Base64 file content") from None
    if len(raw) > MAX_FILE_SIZE:
        raise UnsupportedContentError("Decoded file exceeds the size limit")
    if len(raw) != entry["size"]:
        raise ResponseFormatError("Decoded size does not match GitHub file size")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise UnsupportedContentError("File is not UTF-8 text") from None
    if "\x00" in text:
        raise UnsupportedContentError("File contains binary NUL bytes")
    return {"path": entry["path"], "sha": entry["sha"], "size": entry["size"], "text": text}
