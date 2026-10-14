"""Domain errors shared by the HTTP client and tools."""


class GitHubError(Exception):
    """Base error; metadata lets callers decide whether to retry."""

    def __init__(self, message, *, status_code=None, retryable=False,
                 retry_after=None, rate_limit_reset=None, request_id=None):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after
        self.rate_limit_reset = rate_limit_reset
        self.request_id = request_id


class InvalidArgumentError(GitHubError):
    pass


class AuthenticationError(GitHubError):
    pass


class PermissionDeniedError(GitHubError):
    pass


class RateLimitError(GitHubError):
    pass


class NotFoundError(GitHubError):
    pass


class EmptyRepositoryError(NotFoundError):
    pass


class NetworkError(GitHubError):
    pass


class ServiceError(GitHubError):
    pass


class ResponseFormatError(GitHubError):
    pass


class WrongContentTypeError(GitHubError):
    pass


class UnsupportedContentError(GitHubError):
    pass


class ContentDecodeError(ResponseFormatError):
    pass
