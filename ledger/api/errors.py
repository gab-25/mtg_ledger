"""API errors: DRF's format, plus a machine-readable code.

Validation errors stay DRF's {"field": ["message"], "non_field_errors": [...]}. Every other error is
{"detail": str, "code": str, "help_url"?: str}: e.g. not_authenticated (403), permission_denied (403, also a
missing CSRF token), not_found, method_not_allowed, parse_error, closed, blocked, service_missing, service_error.
"""

from rest_framework import exceptions, status
from rest_framework.views import exception_handler as drf_exception_handler


class Closed(exceptions.APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "This scan session was already saved."
    default_code = "closed"


class Blocked(exceptions.APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "This action is not possible right now."
    default_code = "blocked"


class ServiceError(exceptions.APIException):
    """An external service (OpenRouter, CardTrader) failed: the user can retry."""

    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = "An external service failed — try again."
    default_code = "service_error"


class ServiceMissing(exceptions.APIException):
    """A service is not configured; help_url points to where the user can set it up, when they can."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "A required service is not available right now — try again later."
    default_code = "service_missing"

    def __init__(self, detail=None, help_url=""):
        super().__init__(detail)
        self.help_url = help_url


def exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is None or isinstance(exc, exceptions.ValidationError) or not isinstance(response.data, dict):
        return response
    detail = response.data.get("detail")
    if detail is not None:
        response.data = {"detail": str(detail), "code": getattr(detail, "code", None) or "error"}
        if help_url := getattr(exc, "help_url", ""):
            response.data["help_url"] = help_url
    return response
