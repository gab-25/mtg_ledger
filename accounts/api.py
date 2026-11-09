"""The account API (/api/me/, /api/auth/, /api/account/): who is logged in, login/logout, profile and CardTrader token.

Authentication is the Django session; CSRF comes from the csrftoken cookie, sent back as X-CSRFToken.
"""

from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from ledger import services as ledger_services
from ledger.api.common import form_errors, messages_response, msg
from ledger.cardtrader import CardTraderError
from ledger.models import Listing, Order

from .forms import CardTraderTokenForm, ProfileForm

TAGS = ["account"]


def _user(user):
    return {
        "username": user.get_username(),
        "first_name": user.first_name,
        "last_name": user.last_name,
        "full_name": user.get_full_name(),
        "email": user.email,
        "is_staff": user.is_staff,
        "date_joined": user.date_joined,
        "last_login": user.last_login,
    }


@extend_schema(tags=TAGS, summary="The logged-in user and the sidebar counters")
@api_view(["GET"])
def me(request):
    user = request.user
    return Response({
        "user": _user(user),
        "counts": {
            "pending_changes": Listing.objects.owned_by(user).filter(status=Listing.Status.TO_PUBLISH).count(),
            "orders_to_ship": Order.objects.owned_by(user).filter(state=Order.State.PAID).count(),
        },
    })


@extend_schema(tags=TAGS, summary="Set the CSRF cookie (for the login form)")
@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
@ensure_csrf_cookie
def csrf(request):
    return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=TAGS, summary="Log in with username and password")
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@csrf_protect
def login_view(request):
    form = AuthenticationForm(request, data=request.data)
    if not form.is_valid():
        raise form_errors(form)
    login(request, form.get_user())
    return Response({"ok": True})


@extend_schema(tags=TAGS, summary="Log out")
@api_view(["POST"])
def logout_view(request):
    logout(request)
    return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=TAGS, summary="Profile page data: the CardTrader connection")
@api_view(["GET"])
def account(request):
    user = request.user
    cardtrader = ledger_services.get_cardtrader(user)
    return Response({
        "cardtrader": {
            "name": cardtrader.name,
            "last_sync": cardtrader.last_sync,
            "token_saved": bool(cardtrader.api_token),
        },
        "shares_token": user.is_superuser and ledger_services.service_account_user() == user,
    })


@extend_schema(tags=TAGS, summary="Change first name, last name and email")
@api_view(["PUT"])
def profile(request):
    form = ProfileForm(request.data, instance=request.user)
    if not form.is_valid():
        raise form_errors(form)
    form.save()
    return messages_response(msg("success", "Profile updated."))


@extend_schema(tags=TAGS, summary="Change the password (the session stays logged in)")
@api_view(["POST"])
def password(request):
    form = PasswordChangeForm(request.user, request.data)
    if not form.is_valid():
        raise form_errors(form)
    form.save()
    update_session_auth_hash(request, form.user)  # stay logged in
    return messages_response(msg("success", "Password changed."))


@extend_schema(tags=TAGS, summary="Connect (PUT) or disconnect (DELETE) CardTrader with an API token")
@api_view(["PUT", "DELETE"])
def cardtrader_token(request):
    user = request.user
    if request.method == "DELETE":
        ledger_services.remove_api_token(user)
        return messages_response(msg("success", "CardTrader token removed."))
    form = CardTraderTokenForm(request.data)
    if form.is_valid():
        try:
            info = ledger_services.set_api_token(user, form.cleaned_data["token"])
        except CardTraderError as exc:
            form.add_error("token", f"CardTrader rejected this token: {exc}")
        except ledger_services.TokenInUse as exc:
            form.add_error("token", str(exc))
        else:
            app = f" (app “{info['name']}”)" if info.get("name") else ""
            return messages_response(msg("success", f"CardTrader connected{app}."))
    raise form_errors(form)
