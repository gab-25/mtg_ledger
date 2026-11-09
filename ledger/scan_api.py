"""JSON API of the camera scan flow (/api/scan/), used by the React scan app served at /scan/.

Authentication is the Django session; CSRF comes from the csrftoken cookie, sent back as X-CSRFToken.
Every error is {"error": str, "code": str, "help_url"?: str}; decimals travel as strings.
"""

import functools
import json
import logging
import re
from decimal import Decimal, InvalidOperation

import segno
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views.decorators.csrf import ensure_csrf_cookie

from . import catalog, pricing, recognition, scan, services
from .cardtrader import CardTraderError
from .models import Blueprint, Condition, Expansion, Language, ScanLine, ScanSession
from .openrouter import OpenRouterError

MAX_FRAME_BYTES = 2 * 1024 * 1024
SEARCH_LIMIT = 20
PHONE_UA = re.compile(r"Mobi|Android|iPhone|iPod", re.IGNORECASE)
logger = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status, code, error, help_url=""):
        super().__init__(error)
        self.status, self.code, self.error, self.help_url = status, code, error, help_url


def _error(status, code, error, help_url=""):
    body = {"error": error, "code": code}
    if help_url:
        body["help_url"] = help_url
    return JsonResponse(body, status=status)


def api(*methods):
    """A JSON endpoint: 401 instead of the login redirect, 405 on other methods, ApiError/Http404 as JSON."""

    def decorator(view):
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return _error(401, "login", "Log in to continue.")
            if request.method not in methods:
                resp = _error(405, "invalid", f"{request.method} is not allowed here.")
                resp["Allow"] = ", ".join(methods)
                return resp
            try:
                return view(request, *args, **kwargs)
            except ApiError as exc:
                return _error(exc.status, exc.code, exc.error, exc.help_url)
            except Http404:
                return _error(404, "not_found", "Not found.")

        return wrapper

    return decorator


def _body(request):
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "invalid", "The request body is not valid JSON.")
    if not isinstance(data, dict):
        raise ApiError(400, "invalid", "The request body must be a JSON object.")
    return data


def _closed():
    return ApiError(409, "closed", "This scan session was already saved.")


# --- helpers kept from the server-rendered scan pages ---


def _is_phone(request):
    """Camera scanning is for smartphones (autofocus rear camera); desktops and webcams are not supported."""
    return bool(PHONE_UA.search(request.headers.get("User-Agent", "")))


def _qr_svg(url):
    """Inline SVG QR code (dark modules on white, readable by phone cameras in both themes)."""
    return segno.make(url, error="m").svg_inline(scale=6, border=2, dark="#121318", light="#FFFFFF")


def _openrouter_configured():
    """OpenRouter is a server setting: when it is missing, say so in the server console, not to the user."""
    if services.openrouter_key():
        return True
    logger.warning("OpenRouter is not configured: card recognition needs the OPENROUTER_API_KEY environment variable.")
    return False


def _missing_services():
    """Connections the user must set up in the profile before scanning: [(message, profile link)]."""
    profile = reverse("accounts:profile")
    missing = []
    if not services.api_token():
        missing.append(("CardTrader is not connected: the card catalog and prices need its API token.",
                        f"{profile}#cardtrader"))
    return missing


def _language(value, default=""):
    value = value.upper() if isinstance(value, str) else ""
    return value if value in Language.values else default


def _search_expansion(code, session, warnings):
    """The expansion to search in: set=<code> (loaded on demand), else the session's, else None (all loaded)."""
    code = (code or "").strip().lower()
    if code == "all":
        return None
    if code:
        expansion = catalog.expansion_by_code(code)
        if expansion is not None:
            try:
                with services.make_client() as client:
                    catalog.ensure_blueprints(client, expansion)
            except CardTraderError as exc:
                warnings.append(f"Could not load {expansion}: {exc}")
            return expansion
    return session.expansion


def _search(expansion, q):
    qs = Blueprint.objects.select_related("expansion")
    if expansion is not None:
        qs = qs.filter(expansion=expansion)
    q = q.strip()
    return list(qs.filter(name__icontains=q).order_by("name", "ct_id")[:SEARCH_LIMIT]) if q else []


# --- serializers ---


def _money(value):
    return None if value is None else str(value)


def _expansion(e):
    return None if e is None else {"id": e.pk, "code": e.code, "name": e.name}


def _blueprint(b):
    if b is None:
        return None
    return {
        "id": b.ct_id, "name": b.name, "collector_number": b.collector_number, "version": b.version,
        "rarity": b.rarity, "image_url": b.image_url, "expansion": _expansion(b.expansion),
    }


def _choices(choices):
    return [{"code": code, "label": label} for code, label in choices]


def _line(line, session=None):
    if line is None:
        return None
    if session is not None:
        line.session = session
    candidates = []
    if line.needs_confirmation and line.candidates:
        by_id = {b.ct_id: b for b in Blueprint.objects.filter(ct_id__in=line.candidates).select_related("expansion")}
        candidates = [_blueprint(by_id[i]) for i in line.candidates if i in by_id]
    return {
        "uuid": str(line.uuid), "blueprint": _blueprint(line.blueprint), "language": line.language,
        "foil": line.foil, "condition": line.condition, "quantity": line.quantity, "price": _money(line.price),
        "for_sale": line.for_sale, "listed": line.listed, "needs_confirmation": line.needs_confirmation,
        "frame_url": reverse("ledger:scan_api_frame", args=[line.uuid]) if line.frame else None,
        "candidates": candidates,
    }


def _count(session):
    return session.lines.aggregate(n=Sum("quantity"))["n"] or 0


def _session(request, session):
    last = (session.lines.filter(needs_confirmation=False)
            .select_related("blueprint__expansion").order_by("-updated_at", "-id").first())
    phone_url = request.build_absolute_uri(reverse("ledger:scan_app", args=[f"{session.uuid}/"]))
    return {
        "uuid": str(session.uuid), "status": session.status, "expansion": _expansion(session.expansion),
        "default_condition": session.default_condition, "sale_threshold": _money(session.sale_threshold),
        "count": _count(session), "last_line": _line(last, session), "is_phone": _is_phone(request),
        "phone_url": phone_url, "phone_qr_svg": _qr_svg(phone_url), "phone_secure": request.is_secure(),
    }


def _line_result(line, session):
    return JsonResponse({"line": _line(line, session), "count": _count(session)})


# --- lookups ---


def _get_session(uid, *, open_only=True):
    session = get_object_or_404(ScanSession.objects.select_related("expansion"), uuid=uid)
    if open_only and session.status != ScanSession.Status.OPEN:
        raise _closed()
    return session


def _get_line(uid):
    line = get_object_or_404(ScanLine.objects.select_related("session__expansion", "blueprint__expansion"), uuid=uid)
    if line.session.status != ScanSession.Status.OPEN:
        raise _closed()
    return line


def _pick(data):
    """(blueprint, language) picked by hand, or a 400."""
    blueprint_id = data.get("blueprint_id")
    blueprint = None
    if isinstance(blueprint_id, int) and not isinstance(blueprint_id, bool):
        blueprint = Blueprint.objects.select_related("expansion").filter(ct_id=blueprint_id).first()
    language = _language(data.get("language"))
    if blueprint is None or not language:
        raise ApiError(400, "invalid", "Pick a card and its language.")
    return blueprint, language


# --- endpoints ---


@api("GET")
def setup(request):
    warnings = []
    if not Expansion.objects.exists() and services.api_token():
        try:
            with services.make_client() as client:
                catalog.sync_expansions(client)
        except CardTraderError as exc:
            warnings.append(f"Could not load the expansions from CardTrader: {exc}")
    last = ScanSession.objects.order_by("-created_at", "-id").first()
    return JsonResponse({
        "expansions": [_expansion(e) for e in Expansion.objects.all()],
        "conditions": _choices(Condition.choices),
        "languages": _choices(Language.choices),
        "defaults": {
            "expansion_id": last.expansion_id if last else None,
            "condition": last.default_condition if last else Condition.NEAR_MINT,
        },
        "missing": [{"message": message, "url": url} for message, url in _missing_services()],
        "warnings": warnings,
    })


@api("POST")
def sessions(request):
    data = _body(request)
    expansion = None
    expansion_id = data.get("expansion_id")
    if expansion_id is not None:
        if not isinstance(expansion_id, int) or isinstance(expansion_id, bool):
            raise ApiError(400, "invalid", "Pick an expansion.")
        expansion = get_object_or_404(Expansion, pk=expansion_id)
    condition = data.get("condition")
    if condition not in Condition.values:
        condition = Condition.NEAR_MINT
    session = scan.start_session(expansion, condition)
    warnings = []
    if expansion is not None:
        try:
            with services.make_client() as client:
                catalog.ensure_blueprints(client, expansion)
                my_id = (client.info() or {}).get("user_id")
                pricing.fill_expansion(client, expansion, my_id)
        except CardTraderError as exc:
            warnings.append(f"Could not preload {expansion} from CardTrader: {exc}")
    return JsonResponse({"session": _session(request, session), "warnings": warnings}, status=201)


@api("GET")
def session_detail(request, uid):
    return JsonResponse({"session": _session(request, _get_session(uid, open_only=False))})


def _capture_result(result, message, session, line=None, status=200):
    return JsonResponse({"result": result, "message": message, "line": _line(line, session), "count": _count(session)},
                        status=status)


@api("POST")
def captures(request, uid):
    session = _get_session(uid)
    frame = request.FILES.get("frame")
    capture_id = request.POST.get("capture_id", "")[:64]
    if frame is None or frame.size > MAX_FRAME_BYTES:
        raise ApiError(400, "invalid", "Missing or oversized photo — try again")
    if not _openrouter_configured():
        raise ApiError(503, "service_missing", "Card recognition is not available right now — try again later")
    missing = _missing_services()
    if missing:
        message, link = missing[0]
        raise ApiError(503, "service_missing", message, link)

    capture, created = scan.reserve_capture(session, capture_id) if capture_id else (None, True)
    if not created:
        if not capture.done:  # the first upload of this photo is still being recognised
            return _capture_result("busy", "Still recognising the previous photo…", session, status=202)
        if capture.line is None:  # recognised, then undone or removed
            return _capture_result("handled", "Already handled", session)
        done = capture.line
    else:
        image = frame.read()
        try:
            with services.make_openrouter() as model:
                rec = model.recognize_card(image)
            if not rec.name and not rec.collector_number:  # an empty table, a hand, a blurred frame
                scan.release_capture(capture)
                return _capture_result("no_card", "No card in the picture — frame a card", session)
            with services.make_client() as client:
                found = recognition.match(client, rec, session.expansion)
                done, _ = scan.add_capture(session, client, rec, found, frame=image, capture=capture)
        except (OpenRouterError, CardTraderError) as exc:
            scan.release_capture(capture)
            reason = str(exc)[:160]
            raise ApiError(502, "service_error", f"Couldn't recognise the card ({reason}) — take the photo again")
        except Exception:
            scan.release_capture(capture)
            raise

    if done.needs_confirmation:
        return _capture_result("confirm", "Which one is it?", session, done)
    copies = "copy" if done.quantity == 1 else "copies"
    return _capture_result("added", f"Added · {done.quantity} {copies}", session, done)


@api("GET")
def search(request, uid):
    session = _get_session(uid, open_only=False)
    warnings = []
    where = _search_expansion(request.GET.get("set"), session, warnings)
    return JsonResponse({
        "results": [_blueprint(b) for b in _search(where, request.GET.get("q", ""))],
        "where": where.code if where is not None else None,
        "warnings": warnings,
    })


@api("POST")
def manual(request, uid):
    session = _get_session(uid)
    data = _body(request)
    blueprint, language = _pick(data)
    foil = data.get("foil") is True
    try:
        with services.make_client() as client:
            line = scan.add_manual(session, client, blueprint, language, foil)
    except CardTraderError:
        line = scan.add_manual(session, None, blueprint, language, foil)
    return _line_result(line, session)


def _review(request, session):
    lines = list(session.lines.select_related("blueprint__expansion").order_by("needs_confirmation", "-updated_at"))
    for line in lines:
        line.session = session
    confirmed = [l for l in lines if not l.needs_confirmation]
    pending = [l for l in lines if l.needs_confirmation]
    return JsonResponse({
        "session": _session(request, session),
        "lines": [_line(l) for l in confirmed],
        "pending": [_line(l) for l in pending],
        "copies": sum(l.quantity for l in lines),
        "unique": len(confirmed),
        "listed": sum(l.quantity for l in confirmed if l.listed),
    })


@api("GET")
def review(request, uid):
    return _review(request, _get_session(uid, open_only=False))


@api("PUT")
def threshold(request, uid):
    session = _get_session(uid)
    raw = _body(request).get("threshold", "")
    if not isinstance(raw, str):
        raise ApiError(400, "invalid", "Enter the threshold as a price, e.g. 2.50.")
    raw = raw.strip().replace(",", ".")
    try:
        value = Decimal(raw).quantize(Decimal("0.01")) if raw else None
    except InvalidOperation:
        raise ApiError(400, "invalid", "Enter the threshold as a price, e.g. 2.50.")
    if value is not None and (not value.is_finite() or value < 0):
        value = None
    session.sale_threshold = value
    session.save(update_fields=["sale_threshold", "updated_at"])
    return _review(request, session)


@api("POST")
def save(request, uid):
    session = _get_session(uid)
    try:
        saved, listed = scan.save_session(session)
    except scan.ScanError as exc:
        if "already saved" in str(exc):  # saved by another request in the meantime
            raise _closed()
        raise ApiError(409, "blocked", str(exc))
    messages.success(request, f"Saved {saved} cards, {listed} queued for sale.")
    return JsonResponse({"saved": saved, "listed": listed, "redirect": reverse("ledger:collection")})


_FIELDS = {"foil", "condition", "language", "quantity", "for_sale"}


def _changes(data):
    """The validated PATCH fields, or a 400."""
    unknown = set(data) - _FIELDS
    if unknown:
        raise ApiError(400, "invalid", f"Unknown field: {', '.join(sorted(unknown))}.")
    if not data:
        raise ApiError(400, "invalid", "Nothing to change.")
    changes = {}
    if "foil" in data:
        if not isinstance(data["foil"], bool):
            raise ApiError(400, "invalid", "foil must be true or false.")
        changes["foil"] = data["foil"]
    if "condition" in data:
        if data["condition"] not in Condition.values:
            raise ApiError(400, "invalid", "Unknown condition.")
        changes["condition"] = data["condition"]
    if "language" in data:
        if not _language(data["language"]):
            raise ApiError(400, "invalid", "Unknown language.")
        changes["language"] = _language(data["language"])
    if "quantity" in data:
        quantity = data["quantity"]
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
            raise ApiError(400, "invalid", "quantity must be a whole number, 0 or more.")
        changes["quantity"] = quantity
    if "for_sale" in data:
        if data["for_sale"] not in (True, False, None):
            raise ApiError(400, "invalid", "for_sale must be true, false or null.")
        changes["for_sale"] = data["for_sale"]
    return changes


@api("PATCH")
def line_detail(request, uid):
    line = _get_line(uid)
    session = line.session
    return _line_result(scan.update_line(line, **_changes(_body(request))), session)


@api("POST")
def line_undo(request, uid):
    line = _get_line(uid)
    session = line.session
    scan.undo(line)
    survivor = ScanLine.objects.select_related("blueprint__expansion").filter(pk=line.pk).first() if line.pk else None
    return _line_result(survivor, session)


@api("POST")
def line_confirm(request, uid):
    line = _get_line(uid)
    session = line.session
    if not line.needs_confirmation:
        return _line_result(line, session)
    data = _body(request)
    blueprint, language = _pick(data)
    condition = data.get("condition")
    line.foil = data.get("foil") is True
    line.condition = condition if condition in Condition.values else session.default_condition
    try:
        with services.make_client() as client:
            line = scan.confirm_line(line, client, blueprint, language)
    except CardTraderError:
        line = scan.confirm_line(line, None, blueprint, language)
    return _line_result(line, session)


@api("GET")
def line_frame(request, uid):
    line = get_object_or_404(ScanLine, uuid=uid)
    if not line.frame:
        raise Http404
    return HttpResponse(bytes(line.frame), content_type="image/jpeg")


# --- the React app ---


@login_required
@ensure_csrf_cookie
def scan_app(request, rest=""):
    """Every /scan/... URL serves the built SPA's index.html; the app routes on the client."""
    index = settings.SCAN_APP_DIR / "index.html"
    try:
        html = index.read_bytes()
    except OSError:
        return HttpResponse("Scan app not built: set SCAN_APP_DIR (see README)", status=503,
                            content_type="text/plain; charset=utf-8")
    return HttpResponse(html, content_type="text/html; charset=utf-8")
