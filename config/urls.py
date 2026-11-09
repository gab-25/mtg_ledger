from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.contrib.auth import views as auth_views
from django.templatetags.static import static
from django.urls import include, path
from django.views.generic import RedirectView
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    path("favicon.ico", RedirectView.as_view(url=static("theme/favicon.svg"))),
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("account/", include("accounts.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="api_schema"),
    path("api/docs/", login_required(SpectacularSwaggerView.as_view(url_name="api_schema")), name="api_docs"),
    path("", include("ledger.urls")),
]
