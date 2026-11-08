from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.shortcuts import redirect, render
from django.urls import reverse

from ledger import services as ledger_services
from ledger.cardtrader import CardTraderError

from .forms import CardTraderTokenForm, ProfileForm


@login_required
def profile(request):
    user = request.user
    action = request.POST.get("action") if request.method == "POST" else None

    def data(name):
        return request.POST if action == name else None

    profile_form = ProfileForm(data("profile"), instance=user, prefix="profile")
    password_form = PasswordChangeForm(user, data("password"), prefix="password")
    token_form = CardTraderTokenForm(data("cardtrader"), prefix="cardtrader")
    cardtrader_url = reverse("accounts:profile") + "#cardtrader"

    if action == "profile" and profile_form.is_valid():
        profile_form.save()
        messages.success(request, "Profile updated.")
        return redirect("accounts:profile")
    if action == "password" and password_form.is_valid():
        password_form.save()
        update_session_auth_hash(request, password_form.user)  # stay logged in
        messages.success(request, "Password changed.")
        return redirect("accounts:profile")
    if action == "cardtrader" and token_form.is_valid():
        try:
            info = ledger_services.set_api_token(token_form.cleaned_data["token"])
        except CardTraderError as exc:
            token_form.add_error("token", f"CardTrader rejected this token: {exc}")
        else:
            app = f" (app “{info['name']}”)" if info.get("name") else ""
            messages.success(request, f"CardTrader connected{app}.")
            return redirect(cardtrader_url)
    if action == "cardtrader_remove":
        ledger_services.remove_api_token()
        messages.success(request, "CardTrader token removed.")
        return redirect(cardtrader_url)

    cardtrader = ledger_services.get_cardtrader()
    return render(request, "accounts/profile.html", {
        "nav": "profile",
        "profile_form": profile_form,
        "password_form": password_form,
        "token_form": token_form,
        "cardtrader": cardtrader,
        "token_saved": bool(cardtrader.api_token),
        "token_from_env": not cardtrader.api_token and bool(getattr(settings, "CARDTRADER_API_TOKEN", "")),
    })
