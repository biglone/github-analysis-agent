"""Synchronous Contents API client; no redirects or automatic retries."""
import logging
import math
import os
import re
from urllib.parse import quote

import httpx

from .config import load_project_env
from .errors import (
    AuthenticationError, EmptyRepositoryError, GitHubError, InvalidArgumentError,
    NetworkError, NotFoundError, PermissionDeniedError, RateLimitError,
    ResponseFormatError, ServiceError,
)

logger = logging.getLogger(__name__)


def validate_arguments(owner, repo, path, ref):
    for label, value in (("owner", owner), ("repo", repo)):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", value) or value in (".", ".."):
            raise InvalidArgumentError(f"{label} must be a single repository identifier")
    if not isinstance(path, str) or path.startswith("/") or "\\" in path or any(ord(c) < 32 for c in path):
        raise InvalidArgumentError("path must be a repository-relative path")
    if path and any(part in ("", ".", "..") for part in path.split("/")):
        raise InvalidArgumentError("path must not contain empty, '.' or '..' segments")
    if ref is not None and (not isinstance(ref, str) or not ref.strip() or any(ord(c) < 32 for c in ref)):
        raise InvalidArgumentError("ref must be a nonempty branch, tag or commit")


class GitHubClient:
    def __init__(self, token=None, *, timeout=10.0, transport=None):
        load_project_env()
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout <= 0:
            raise InvalidArgumentError("timeout must be a positive finite number")
        token = os.environ.get("GITHUB_TOKEN") if token is None else token
        if token is not None and (not isinstance(token, str) or any(ord(c) < 32 for c in token)):
            raise InvalidArgumentError("invalid token format")
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2026-03-10"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(base_url="https://api.github.com", headers=headers,
                                  timeout=timeout, transport=transport, follow_redirects=False)

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def get_contents(self, owner, repo, path="", ref=None, *, timeout=None):
        validate_arguments(owner, repo, path, ref)
        encoded = "/".join(quote(segment, safe="") for segment in path.split("/"))
        url = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/contents"
        if path:
            url += "/" + encoded
        request_options = {}
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
                raise InvalidArgumentError("timeout must be a positive finite number")
            request_options["timeout"] = timeout
        try:
            response = self._http.get(url, params={} if ref is None else {"ref": ref}, **request_options)
        except httpx.RequestError:
            # Do not propagate request/header details or a token-bearing exception.
            raise NetworkError("GitHub request timed out or could not connect", retryable=True) from None
        logger.info("GitHub contents status=%s request_id=%s",
                    response.status_code, response.headers.get("x-github-request-id"))
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.is_error or not response.is_success:
            message = data.get("message", "") if isinstance(data, dict) else ""
            message = message if isinstance(message, str) else ""
            meta = dict(status_code=response.status_code,
                        request_id=response.headers.get("x-github-request-id"),
                        retry_after=response.headers.get("retry-after"),
                        rate_limit_reset=response.headers.get("x-ratelimit-reset"))
            status = response.status_code
            if status == 401:
                error, text = AuthenticationError, "GitHub token is invalid or expired"
            elif status == 429 or (status == 403 and (
                    response.headers.get("x-ratelimit-remaining") == "0" or
                    response.headers.get("retry-after") is not None or
                    "rate limit" in message.lower() or "abuse" in message.lower())):
                error, text = RateLimitError, "GitHub rate limit exceeded"
                meta["retryable"] = True
            elif status == 403:
                error, text = PermissionDeniedError, "GitHub permission denied"
            elif status == 404 and message.strip().lower() == "this repository is empty.":
                error, text = EmptyRepositoryError, "GitHub repository is empty"
            elif status == 404:
                error, text = NotFoundError, "Repository, path or ref not found, or inaccessible"
            elif status >= 500:
                error, text = ServiceError, "GitHub service failed"
                meta["retryable"] = True
            else:
                error, text = GitHubError, "Unexpected GitHub HTTP status"
            raise error(text, **meta)
        if not isinstance(data, (dict, list)):
            raise ResponseFormatError("GitHub returned invalid JSON or an unexpected JSON type")
        return data
