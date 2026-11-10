"""Helpers shared by the page APIs: OpenAPI docs, flash-style messages, Django form errors, pagination, safe redirects."""

from django import forms
from django.core.paginator import Paginator
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from django.utils.http import url_has_allowed_host_and_scheme
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

PAGE_SIZE = 50


def doc(tags, summary, request=None, parameters=None):
    """OpenAPI entry of a page API view: its body (request; None: no body) and query parameters, a JSON object out."""
    return extend_schema(tags=tags, summary=summary, request=request, parameters=parameters,
                         responses=OpenApiTypes.OBJECT)


def body(name, fields):
    """A request body with these serializer fields (the component is named <name>Request)."""
    return inline_serializer(name, fields)


def form_body(name, form_class, **extra):
    """The request body of a view that validates request.data with a Django form: the form's fields, plus extra."""
    return body(name, {**{key: _form_field(field) for key, field in form_class.base_fields.items()}, **extra})


def _form_field(field):
    kwargs = {"required": field.required}
    if isinstance(field, forms.BooleanField):
        return serializers.BooleanField(required=False)
    if isinstance(field, forms.ChoiceField):
        return serializers.ChoiceField(choices=[c for c in field.choices if c[0] != ""], **kwargs)
    if isinstance(field, forms.DecimalField):  # before IntegerField, its parent
        return serializers.DecimalField(max_digits=field.max_digits, decimal_places=field.decimal_places, **kwargs)
    if isinstance(field, forms.IntegerField):
        return serializers.IntegerField(min_value=field.min_value, max_value=field.max_value, **kwargs)
    if isinstance(field, forms.FileField):
        return serializers.FileField(**kwargs)
    kwargs.update(max_length=field.max_length, allow_blank=not field.required)
    if isinstance(field, forms.EmailField):
        return serializers.EmailField(**kwargs)
    if isinstance(field, forms.CharField):
        if isinstance(field.widget, forms.PasswordInput):
            kwargs["style"] = {"input_type": "password"}
        return serializers.CharField(**kwargs)
    raise TypeError(f"No OpenAPI field for {type(field).__name__}")


def query(name, description, enum=None):
    """A query parameter of a list view (a string, optionally one of enum)."""
    return OpenApiParameter(name, OpenApiTypes.STR, OpenApiParameter.QUERY, description=description, enum=enum)


PAGE = OpenApiParameter("page", OpenApiTypes.INT, OpenApiParameter.QUERY,
                        description="Page number (out of range: the nearest page)")


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
