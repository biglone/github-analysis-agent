"""Safe, stable errors exposed by the DeepSeek model adapter."""


class ModelAdapterError(Exception):
    """Base error for model requests; messages never include SDK response data."""

    def __init__(self, message, *, retryable=False, status_code=None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class MissingAPIKeyError(ModelAdapterError):
    pass


class ModelAuthenticationError(ModelAdapterError):
    pass


class ModelRateLimitError(ModelAdapterError):
    pass


class ModelTimeoutError(ModelAdapterError):
    pass


class ModelNetworkError(ModelAdapterError):
    pass


class ModelServiceError(ModelAdapterError):
    pass


class InvalidToolArgumentsError(ModelAdapterError):
    pass


class ModelResponseError(ModelAdapterError):
    pass
