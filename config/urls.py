from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.templatetags.static import static
from django.urls import include, path, re_path
from django.views.generic import RedirectView
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from theme.views import ui_app

urlpatterns = [
    path("favicon.ico", RedirectView.as_view(url=static("ui/favicon.svg"))),
    path("admin/", admin.site.urls),
    path("", include("accounts.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="api_schema"),
    path("api/docs/", login_required(SpectacularSwaggerView.as_view(url_name="api_schema")), name="api_docs"),
    path("", include("ledger.urls")),
    # Every other URL is a page of the React UI (last: /api/, /admin/ and /static/ are never pages).
    re_path(r"^(?!api/|admin/|static/).*$", ui_app, name="ui"),
]
