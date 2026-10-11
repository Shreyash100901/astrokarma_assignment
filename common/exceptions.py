from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.views import exception_handler


class DomainError(APIException):
    """Business-rule violation. Rendered as {"error": ..., "code": ..., **extra}."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "domain_error"

    def __init__(self, message, **extra):
        super().__init__(detail=message, code=self.default_code)
        self.extra = extra


class InvalidTransition(DomainError):
    default_code = "invalid_transition"


class InsufficientBalance(DomainError):
    default_code = "insufficient_balance"


class ActiveSessionExists(DomainError):
    default_code = "active_session_exists"


class Conflict(DomainError):
    status_code = status.HTTP_409_CONFLICT
    default_code = "conflict"


def api_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is not None and isinstance(exc, DomainError):
        response.data = {"error": str(exc.detail), "code": exc.default_code, **exc.extra}
    return response
