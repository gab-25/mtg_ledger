from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import ensure_csrf_cookie


@ensure_csrf_cookie
def ui_app(request):
    """The React UI (gab-25/mtg_ledger_ui): every page URL serves its index.html; the app routes on the client.

    No login here: the UI asks /api/me/ and goes to its login page when nobody is logged in.
    """
    index = settings.UI_APP_DIR / "index.html"
    try:
        html = index.read_bytes()
    except OSError:
        return HttpResponse("UI not built: set UI_APP_DIR (see README)", status=503,
                            content_type="text/plain; charset=utf-8")
    return HttpResponse(html, content_type="text/html; charset=utf-8")
