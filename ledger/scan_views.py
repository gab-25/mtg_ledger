"""Camera scanning: start a session, capture cards from the phone, resolve doubts, review and save."""

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from . import catalog, pricing, recognition, scan, services
from .cardtrader import CardTraderError
from .models import Blueprint, Condition, Expansion, Language, ScanLine, ScanSession
from .openrouter import OpenRouterError

MAX_FRAME_BYTES = 2 * 1024 * 1024
SEARCH_LIMIT = 20


def _open_session(pk):
    session = get_object_or_404(ScanSession.objects.select_related("expansion"), pk=pk)
    return session if session.status == ScanSession.Status.OPEN else None


def _count(session):
    return session.lines.aggregate(n=Sum("quantity"))["n"] or 0


def _next(request, default):
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return nxt
    return default


def _wants_json(request):
    return "application/json" in request.headers.get("Accept", "")


def _line_json(line):
    bp = line.blueprint
    return {
        "id": line.pk,
        "name": bp.name if bp else "",
        "set_code": bp.expansion.code.upper() if bp else "",
        "number": bp.collector_number if bp else "",
        "condition": line.condition,
        "language": line.language,
        "foil": line.foil,
        "quantity": line.quantity,
        "image_url": bp.image_url if bp else "",
    }


def _search_expansion(request, session):
    """The expansion to search in: ?set=<code> (loaded on demand), else the session's, else None (all loaded)."""
    code = (request.GET.get("set") or "").strip().lower()
    if code == "all":
        return None
    if code:
        expansion = catalog.expansion_by_code(code)
        if expansion is not None:
            try:
                with services.make_client() as client:
                    catalog.ensure_blueprints(client, expansion)
            except CardTraderError as exc:
                messages.warning(request, f"Could not load {expansion}: {exc}")
            return expansion
    return session.expansion


def _search(expansion, q):
    qs = Blueprint.objects.select_related("expansion")
    if expansion is not None:
        qs = qs.filter(expansion=expansion)
    q = q.strip()
    return list(qs.filter(name__icontains=q).order_by("name", "ct_id")[:SEARCH_LIMIT]) if q else []


def _language(value, default=""):
    value = (value or "").upper()
    return value if value in Language.values else default


@login_required
def scan_start(request):
    if request.method == "POST":
        expansion = None
        if request.POST.get("expansion"):
            expansion = get_object_or_404(Expansion, pk=request.POST["expansion"])
        condition = request.POST.get("condition")
        if condition not in Condition.values:
            condition = Condition.NEAR_MINT
        session = scan.start_session(expansion, condition)
        if expansion is not None:
            try:
                with services.make_client() as client:
                    catalog.ensure_blueprints(client, expansion)
                    my_id = (client.info() or {}).get("user_id")
                    pricing.fill_expansion(client, expansion, my_id)
            except CardTraderError as exc:
                messages.warning(request, f"Could not preload {expansion} from CardTrader: {exc}")
        return redirect("ledger:scan_camera", session.pk)

    if not Expansion.objects.exists():
        try:
            with services.make_client() as client:
                catalog.sync_expansions(client)
        except CardTraderError as exc:
            messages.error(request, f"Could not load the expansions from CardTrader: {exc}")
    last = ScanSession.objects.order_by("-created_at", "-id").first()
    return render(request, "ledger/scan_start.html", {
        "nav": "collection",
        "expansions": Expansion.objects.all(),
        "conditions": Condition.choices,
        "condition": last.default_condition if last else Condition.NEAR_MINT,
        "selected": last.expansion_id if last else None,
    })


@login_required
def scan_camera(request, pk):
    session = _open_session(pk)
    if session is None:
        return redirect("ledger:collection")
    return render(request, "ledger/scan_camera.html", {
        "session": session,
        "count": _count(session),
        "last": session.lines.filter(needs_confirmation=False).select_related("blueprint__expansion").first(),
    })


@login_required
@require_POST
def scan_capture(request, pk):
    session = _open_session(pk)
    if session is None:
        return JsonResponse({"status": "error", "message": "This session is closed."}, status=409)
    frame = request.FILES.get("frame")
    capture_id = request.POST.get("capture_id", "")[:64]
    if frame is None or frame.size > MAX_FRAME_BYTES:
        return JsonResponse({"status": "error", "message": "Missing or oversized photo."}, status=400)

    capture, created = scan.reserve_capture(session, capture_id) if capture_id else (None, True)
    if not created:
        if not capture.done:  # the first upload of this photo is still being recognised
            return JsonResponse({"status": "pending", "count": _count(session)}, status=202)
        done, merged = capture.line, False
        if done is None:  # recognised, then undone or removed
            return JsonResponse({"status": "duplicate", "count": _count(session)})
    else:
        image = frame.read()
        try:
            with services.make_openrouter() as model:
                rec = model.recognize_card(image)
            with services.make_client() as client:
                found = recognition.match(client, rec, session.expansion)
                done, merged = scan.add_capture(session, client, rec, found, frame=image, capture=capture)
        except (OpenRouterError, CardTraderError) as exc:
            scan.release_capture(capture)
            return JsonResponse({"status": "error", "message": str(exc)}, status=502)
        except Exception:
            scan.release_capture(capture)
            raise

    count = _count(session)
    if done.needs_confirmation:
        camera = reverse("ledger:scan_camera", args=[session.pk])
        return JsonResponse({
            "status": "uncertain", "line_id": done.pk, "count": count,
            "resolve_url": f"{reverse('ledger:scan_resolve', args=[done.pk])}?next={camera}",
        })
    return JsonResponse({"status": "added", "line": _line_json(done), "merged": merged, "count": count})


@login_required
@require_POST
def scan_line_update(request, pk):
    line = get_object_or_404(ScanLine.objects.select_related("session"), pk=pk)
    session = line.session
    if session.status != ScanSession.Status.OPEN:
        return redirect("ledger:collection")
    post = request.POST
    if post.get("undo"):
        scan.undo(line)
        line = None
    else:
        changes = {}
        if "foil" in post:
            changes["foil"] = post["foil"] == "1"
        if post.get("condition") in Condition.values:
            changes["condition"] = post["condition"]
        if _language(post.get("language")):
            changes["language"] = _language(post["language"])
        if post.get("quantity", "").isdigit():
            changes["quantity"] = int(post["quantity"])
        if "for_sale" in post:
            changes["for_sale"] = {"1": True, "0": False}.get(post["for_sale"])
        line = scan.update_line(line, **changes)
    if _wants_json(request):
        return JsonResponse({"ok": True, "count": _count(session), "line_id": line.pk if line else None})
    return redirect(_next(request, reverse("ledger:scan_review", args=[session.pk])))


@login_required
def scan_frame(request, pk):
    line = get_object_or_404(ScanLine, pk=pk)
    if not line.frame:
        raise Http404
    return HttpResponse(bytes(line.frame), content_type="image/jpeg")


def _pick(request, session):
    """(blueprint, language, foil, condition) from a resolve/search form, or None."""
    blueprint = Blueprint.objects.filter(ct_id=request.POST.get("blueprint") or 0).first()
    language = _language(request.POST.get("language"))
    if blueprint is None or not language:
        return None
    condition = request.POST.get("condition")
    return blueprint, language, request.POST.get("foil") == "1", (
        condition if condition in Condition.values else session.default_condition
    )


@login_required
def scan_resolve(request, pk):
    line = get_object_or_404(ScanLine.objects.select_related("session__expansion"), pk=pk)
    session = line.session
    if session.status != ScanSession.Status.OPEN:
        return redirect("ledger:collection")
    back = _next(request, reverse("ledger:scan_camera", args=[session.pk]))
    if not line.needs_confirmation:
        return redirect(back)
    error = None
    if request.method == "POST":
        picked = _pick(request, session)
        if picked is None:
            error = "Pick a card and its language."
        else:
            blueprint, language, foil, condition = picked
            line.foil, line.condition = foil, condition
            try:
                with services.make_client() as client:
                    scan.confirm_line(line, client, blueprint, language)
            except CardTraderError:
                scan.confirm_line(line, None, blueprint, language)
            return redirect(back)
    q = request.GET.get("q", "")
    where = _search_expansion(request, session)
    by_id = {b.ct_id: b for b in Blueprint.objects.filter(ct_id__in=line.candidates).select_related("expansion")}
    return render(request, "ledger/scan_resolve.html", {
        "session": session, "line": line, "next": back, "q": q, "error": error,
        "candidates": [by_id[i] for i in line.candidates if i in by_id],
        "results": _search(where, q), "where": where, "expansions": Expansion.objects.all(),
        "languages": Language.choices, "conditions": Condition.choices,
    })


@login_required
def scan_search(request, pk):
    session = _open_session(pk)
    if session is None:
        return redirect("ledger:collection")
    camera = reverse("ledger:scan_camera", args=[session.pk])
    error = None
    if request.method == "POST":
        picked = _pick(request, session)
        if picked is None:
            error = "Pick a card and its language."
        else:
            blueprint, language, foil, _ = picked
            try:
                with services.make_client() as client:
                    scan.add_manual(session, client, blueprint, language, foil)
            except CardTraderError:
                scan.add_manual(session, None, blueprint, language, foil)
            messages.success(request, f"Added {blueprint.name}.")
            return redirect(camera)
    q = request.GET.get("q", "")
    where = _search_expansion(request, session)
    return render(request, "ledger/scan_resolve.html", {
        "session": session, "line": None, "next": camera, "q": q, "error": error,
        "candidates": [], "results": _search(where, q), "where": where, "expansions": Expansion.objects.all(),
        "languages": Language.choices, "conditions": Condition.choices,
    })


@login_required
def scan_review(request, pk):
    session = _open_session(pk)
    if session is None:
        return redirect("ledger:collection")
    review = reverse("ledger:scan_review", args=[session.pk])
    if request.method == "POST":
        if "save" in request.POST:
            try:
                saved, listed = scan.save_session(session)
            except scan.ScanError as exc:
                messages.error(request, str(exc))
                return redirect(review)
            messages.success(request, f"Saved {saved} cards, {listed} queued for sale.")
            return redirect("ledger:collection")
        raw = request.POST.get("threshold", "").strip().replace(",", ".")
        try:
            session.sale_threshold = Decimal(raw).quantize(Decimal("0.01")) if raw else None
        except InvalidOperation:
            messages.error(request, "Enter the threshold as a price, e.g. 2.50.")
        else:
            if session.sale_threshold is not None and session.sale_threshold < 0:
                session.sale_threshold = None
            session.save(update_fields=["sale_threshold", "updated_at"])
        return redirect(review)

    lines = list(session.lines.select_related("blueprint__expansion").order_by("needs_confirmation", "-updated_at"))
    for line in lines:
        line.session = session
    confirmed = [l for l in lines if not l.needs_confirmation]
    pending = [l for l in lines if l.needs_confirmation]
    copies = sum(l.quantity for l in lines)
    return render(request, "ledger/scan_review.html", {
        "session": session, "lines": confirmed, "pending": pending,
        "copies": copies, "unique": len(confirmed),
        "listed": sum(l.quantity for l in confirmed if l.listed),
        "languages": Language.choices, "conditions": Condition.choices,
    })
