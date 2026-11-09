"""Helpers shared by the page APIs: flash-style messages, Django form errors, pagination, safe redirects."""

from django.core.paginator import Paginator
from django.utils.http import url_has_allowed_host_and_scheme
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

PAGE_SIZE = 50


def msg(level, text):
    """One message for the UI, like django.contrib.messages: level is success, warning or error."""
    return {"level": level, "text": text}


def messages_response(*msgs, status=200, **extra):
    return Response({"messages": list(msgs), **extra}, status=status)


def form_errors(form):
    """A Django form's errors as a DRF validation error ({"field": [...], "non_field_errors": [...]})."""
    errors = {("non_field_errors" if field == "__all__" else field): list(messages)
              for field, messages in form.errors.items()}
    return ValidationError(errors)


def page_payload(request, queryset, serialize, per_page=PAGE_SIZE):
    """One page of a queryset with Paginator.get_page semantics: a bad or out-of-range page gives the nearest one."""
    page = Paginator(queryset, per_page).get_page(request.query_params.get("page"))
    return {
        "results": [serialize(obj) for obj in page],
        "count": page.paginator.count,
        "page": page.number,
        "num_pages": page.paginator.num_pages,
    }


def safe_next(request, value, default="/"):
    if value and url_has_allowed_host_and_scheme(value, allowed_hosts={request.get_host()}):
        return value
    return default
