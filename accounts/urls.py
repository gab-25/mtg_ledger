from django.urls import path

from . import api, views

app_name = "accounts"

urlpatterns = [
    path("account/", views.profile, name="profile"),
    path("api/me/", api.me, name="api_me"),
    path("api/auth/csrf/", api.csrf, name="api_csrf"),
    path("api/auth/login/", api.login_view, name="api_login"),
    path("api/auth/logout/", api.logout_view, name="api_logout"),
    path("api/account/", api.account, name="api_account"),
    path("api/account/profile/", api.profile, name="api_profile"),
    path("api/account/password/", api.password, name="api_password"),
    path("api/account/cardtrader-token/", api.cardtrader_token, name="api_token"),
]
