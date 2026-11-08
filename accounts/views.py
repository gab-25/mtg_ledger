from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.shortcuts import redirect, render

from ledger import services as ledger_services

from .forms import ProfileForm


@login_required
def profile(request):
    user = request.user
    action = request.POST.get("action") if request.method == "POST" else None

    profile_form = ProfileForm(request.POST if action == "profile" else None, instance=user, prefix="profile")
    password_form = PasswordChangeForm(user, request.POST if action == "password" else None, prefix="password")

    if action == "profile" and profile_form.is_valid():
        profile_form.save()
        messages.success(request, "Profile updated.")
        return redirect("accounts:profile")
    if action == "password" and password_form.is_valid():
        password_form.save()
        update_session_auth_hash(request, password_form.user)  # stay logged in
        messages.success(request, "Password changed.")
        return redirect("accounts:profile")

    cardtrader = ledger_services.get_cardtrader()
    return render(request, "accounts/profile.html", {
        "nav": "profile",
        "profile_form": profile_form,
        "password_form": password_form,
        "cardtrader": cardtrader,
        "cardtrader_token": bool(ledger_services.api_token(cardtrader)),
    })
