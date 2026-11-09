from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Max, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from . import cardmarket, services
from .cardtrader import CardTraderError
from .catalog import normalize_number
from .forms import CardItemForm, CsvImportForm, ListingForm, ShipForm
from .models import Blueprint, Card, CardmarketPrice, CollectionItem, Color, Condition, Listing, Order, SyncConflict, SyncLog, SyncService
from .presentation import COLOR_FILTERS, ORDER_TABS, item_badge, listing_badge, order_badge, pip

PAGE_SIZE = 50
VALUE = ExpressionWrapper(F("price") * F("quantity"), output_field=DecimalField())


def _query(request, **changes):
    """Current querystring with some params changed (None removes them)."""
    params = request.GET.copy()
    params.pop("page", None)
    for key, value in changes.items():
        if value is None:
            params.pop(key, None)
        else:
            params[key] = value
    encoded = params.urlencode()
    return f"?{encoded}" if encoded else "?"


def _safe_next(request, default):
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return nxt
    return default


# --- Collection ----------------------------------------------------------

@login_required
def collection(request):
    items = CollectionItem.objects.select_related("card", "listing")
    stats = items.aggregate(total=Sum("quantity"), value=Sum(VALUE))
    stats["unique"] = Card.objects.filter(items__isnull=False).distinct().count()
    stats["for_sale"] = (
        Listing.objects.exclude(status=Listing.Status.SOLD).aggregate(n=Sum("quantity"))["n"] or 0
    )

    q = request.GET.get("q", "").strip()
    color = request.GET.get("color", "")
    foil = request.GET.get("foil") == "1"
    set_code = request.GET.get("set", "")
    cond = request.GET.get("cond", "")

    if q:
        items = items.filter(
            Q(card__name__icontains=q) | Q(card__set_code__iexact=q) | Q(card__type_line__icontains=q)
        )
    if color in Color.values:
        items = items.filter(card__color=color)
    if foil:
        items = items.filter(foil=True)
    if set_code:
        items = items.filter(card__set_code=set_code)
    if cond in Condition.values:
        items = items.filter(condition=cond)

    page = Paginator(items, PAGE_SIZE).get_page(request.GET.get("page"))
    rows = [{"item": i, "pip": pip(i.card.color), "badge": item_badge(i.status)} for i in page]

    color_pills = [
        {"label": "All", "href": _query(request, color=None), "active": color not in Color.values, "dot": None}
    ] + [
        {"label": label, "href": _query(request, color=code), "active": color == code, "dot": pip(code)}
        for code, label in COLOR_FILTERS
    ]

    return render(request, "ledger/collection.html", {
        "nav": "collection",
        "stats": stats,
        "rows": rows,
        "page": page,
        "q": q,
        "color_pills": color_pills,
        "foil": foil,
        "foil_href": _query(request, foil=None if foil else "1"),
        "set_code": set_code,
        "cond": cond,
        "sets": Card.objects.filter(items__isnull=False).values_list("set_code", flat=True).distinct().order_by("set_code"),
        "conditions": Condition.choices,
        "color": color,
        "filtered": bool(q or color or foil or set_code or cond),
    })


@login_required
def item_create(request):
    form = CardItemForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        item = form.save()
        messages.success(request, f"Added {item.quantity}× {item.card.name}.")
        if "add_another" in request.POST:
            return redirect("ledger:item_create")
        return redirect("ledger:collection")
    return render(request, "ledger/item_form.html", {"nav": "collection", "form": form, "title": "Add cards"})


@login_required
def item_edit(request, pk):
    item = get_object_or_404(CollectionItem.objects.select_related("card"), pk=pk)
    form = CardItemForm(request.POST or None, instance=item)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Saved {item.card.name}.")
        return redirect("ledger:collection")
    return render(request, "ledger/item_form.html", {
        "nav": "collection", "form": form, "item": item, "title": item.card.name,
        "listing": getattr(item, "listing", None),
    })


@login_required
@require_POST
def item_delete(request, pk):
    item = get_object_or_404(CollectionItem, pk=pk)
    name = item.card.name
    item.delete()
    messages.success(request, f"Removed {name} from the collection.")
    return redirect("ledger:collection")


@login_required
def import_csv(request):
    form = CsvImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            created, updated = services.import_csv(form.cleaned_data["file"])
        except services.CsvImportError as exc:
            form.add_error("file", str(exc))
        else:
            messages.success(request, f"Import complete: {created} new, {updated} updated.")
            return redirect("ledger:collection")
    return render(request, "ledger/import.html", {
        "nav": "collection", "form": form, "columns": services.CSV_FIELDS,
    })


@login_required
def export_csv(request):
    items = CollectionItem.objects.all()
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    stamp = timezone.localdate().isoformat()
    response["Content-Disposition"] = f'attachment; filename="mtg-ledger-{stamp}.csv"'
    services.export_csv(items, response)
    SyncLog.objects.create(message=f"CSV export: collection exported ({items.count():,} rows)")
    return response


# --- For sale ------------------------------------------------------------

@login_required
def sale(request):
    listings = Listing.objects.select_related("item__card")
    active = listings.exclude(status=Listing.Status.SOLD)
    month_start = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    stats = {
        "active": active.count(),
        "value": active.aggregate(v=Sum(VALUE))["v"] or 0,
        "sold_month": listings.filter(status=Listing.Status.SOLD, sold_at__gte=month_start).count(),
        "to_publish": listings.filter(status=Listing.Status.TO_PUBLISH).count(),
    }
    status = request.GET.get("status", "")
    if status in Listing.Status.values:
        listings = listings.filter(status=status)
    cards = [{"l": l, "pip": pip(l.item.card.color), "badge": listing_badge(l.status)} for l in listings]
    return render(request, "ledger/sale.html", {
        "nav": "sale", "stats": stats, "cards": cards, "status": status,
        "statuses": Listing.Status.choices,
    })


@login_required
def cards(request):
    """Search the card database (CardTrader catalog) by name, set, collector number and ownership."""
    q = request.GET.get("q", "").strip()
    set_code = request.GET.get("set", "").strip().lower()
    number = normalize_number(request.GET.get("number", ""))
    owned = request.GET.get("owned") == "1"
    page = None
    if q or set_code or number or owned:
        owned_qty = cardmarket.owned_quantities()
        printings = Blueprint.objects.select_related("expansion", "cm_price").order_by("name", "expansion__code")
        if q:
            printings = printings.filter(Q(name__icontains=q) | Q(expansion__code__iexact=q))
        if set_code:
            printings = printings.filter(expansion__code=set_code)
        if number:
            printings = printings.filter(collector_number=number)
        if owned:
            printings = printings.filter(ct_id__in=owned_qty)
        page = Paginator(printings, PAGE_SIZE).get_page(request.GET.get("page"))
        for bp in page:
            bp.owned_qty = owned_qty.get(bp.ct_id)
    params = {k: v for k, v in (("q", q), ("set", set_code), ("number", number), ("owned", "1" if owned else "")) if v}
    return render(request, "ledger/cards.html", {
        "nav": "cards", "q": q, "set_code": set_code, "number": number, "owned": owned, "page": page,
        "query": urlencode(params),
        "prices_date": CardmarketPrice.objects.aggregate(d=Max("prices_date"))["d"] if page is not None else None,
    })


@login_required
def listing_edit(request, item_pk):
    item = get_object_or_404(CollectionItem.objects.select_related("card"), pk=item_pk)
    listing = getattr(item, "listing", None) or Listing(item=item, price=item.price, quantity=1)
    old_status = listing.status
    form = ListingForm(request.POST or None, instance=listing)
    if request.method == "POST" and form.is_valid():
        listing = form.save(commit=False)
        if listing.status == Listing.Status.SOLD and old_status != Listing.Status.SOLD:
            listing.sold_at = timezone.now()
        elif (
            listing.pk
            and listing.status == Listing.Status.PUBLISHED
            and "status" not in form.changed_data
            and form.changed_data
        ):
            # Edited locally: CardTrader needs the new values.
            listing.status = Listing.Status.TO_PUBLISH
        listing.save()
        messages.success(request, f"Listing for {item.card.name} saved.")
        return redirect("ledger:sale")
    return render(request, "ledger/listing_form.html", {
        "nav": "sale", "form": form, "item": item, "listing": listing,
        "cm_suggestion": cardmarket.suggested_price(item.card, item.foil),
    })


@login_required
@require_POST
def listing_delete(request, item_pk):
    listing = get_object_or_404(Listing.objects.select_related("item__card"), item__pk=item_pk)
    try:
        services.remove_listing(listing)
    except CardTraderError as exc:
        messages.error(request, f"Listing not removed: {exc}")
        return redirect("ledger:listing_edit", item_pk=item_pk)
    messages.success(request, "Listing removed.")
    return redirect("ledger:sale")


@login_required
@require_POST
def publish(request):
    try:
        result = services.publish_pending()
    except CardTraderError as exc:
        messages.error(request, f"Publish failed: {exc}")
    else:
        n = result.published
        messages.success(request, f"Published {n} listing{'s' if n != 1 else ''} to CardTrader.")
        for listing, reason in result.failed:
            messages.warning(request, f"{listing.item.card.name}: {reason}")
    return redirect(_safe_next(request, "ledger:sale"))


@login_required
@require_POST
def match_prices(request):
    try:
        count = services.match_market_prices()
    except CardTraderError as exc:
        messages.error(request, f"Could not read market prices: {exc}")
        return redirect("ledger:sale")
    messages.success(request, f"{count} prices matched to market." if count else "All prices already match the market.")
    return redirect("ledger:sale")


# --- Sync ----------------------------------------------------------------

@login_required
def sync(request):
    services.get_cardtrader()
    connections = SyncService.objects.annotate(
        open_conflicts=Count("conflicts", filter=Q(conflicts__resolution=SyncConflict.Resolution.OPEN))
    )
    connections = list(connections)
    for s in connections:
        s.token_ok = bool(services.api_token(s))
    conflicts = SyncConflict.objects.filter(resolution=SyncConflict.Resolution.OPEN).select_related("item__card", "service")
    return render(request, "ledger/sync.html", {
        "nav": "sync",
        "connections": connections,
        "conflicts": conflicts,
        "log": SyncLog.objects.all()[:20],
    })


@login_required
@require_POST
def sync_all(request):
    try:
        conflicts = services.sync_all()
    except CardTraderError as exc:
        messages.error(request, f"Sync failed: {exc}")
    else:
        messages.success(request, f"Sync complete: {conflicts} conflict{'s' if conflicts != 1 else ''} to resolve.")
    return redirect(_safe_next(request, "ledger:sync"))


@login_required
@require_POST
def resolve(request, pk):
    conflict = get_object_or_404(SyncConflict, pk=pk, resolution=SyncConflict.Resolution.OPEN)
    keep = request.POST.get("keep")
    if keep not in (SyncConflict.Resolution.LEDGER, SyncConflict.Resolution.REMOTE):
        messages.error(request, "Choose which source to keep.")
    else:
        services.resolve_conflict(conflict, keep)
        messages.success(request, f"Conflict on {conflict.item.card.name} resolved.")
    return redirect("ledger:sync")



# --- Shipments -----------------------------------------------------------

@login_required
def shipments(request):
    tab = request.GET.get("tab", "to_ship")
    if tab not in ORDER_TABS:
        tab = "to_ship"
    orders = Order.objects.prefetch_related("items")
    month_start = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    to_ship = orders.filter(state=Order.State.PAID)
    stats = {
        "to_ship": to_ship.count(),
        "items_to_ship": to_ship.aggregate(n=Sum("size"))["n"] or 0,
        "shipped_month": orders.filter(sent_at__gte=month_start).count(),
        "revenue_month": orders.filter(paid_at__gte=month_start)
        .exclude(state__in=[Order.State.CANCELED, Order.State.CLOSED])
        .aggregate(v=Sum("total"))["v"] or 0,
    }
    tabs = [
        {"key": key, "label": label, "count": orders.filter(state__in=states).count(), "active": key == tab}
        for key, (label, states) in ORDER_TABS.items()
    ]
    shown = orders.filter(state__in=ORDER_TABS[tab][1])
    if tab == "to_ship":
        shown = shown.order_by("packing_number", "paid_at")
    rows = [{"o": o, "badge": order_badge(o.state)} for o in shown[:200]]
    return render(request, "ledger/shipments.html", {
        "nav": "shipments", "stats": stats, "tabs": tabs, "rows": rows, "tab": tab,
        "service": services.get_cardtrader(),
    })


@login_required
def order_detail(request, pk):
    order = get_object_or_404(Order.objects.prefetch_related("items__listing__item__card"), pk=pk)
    form = ShipForm(initial={"tracking_code": order.tracking_code})
    return render(request, "ledger/order_detail.html", {
        "nav": "shipments", "order": order, "form": form, "badge": order_badge(order.state),
    })


@login_required
@require_POST
def order_ship(request, pk):
    order = get_object_or_404(Order, pk=pk)
    form = ShipForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Invalid tracking code.")
        return redirect("ledger:order_detail", pk=pk)
    try:
        if "save_tracking" in request.POST:
            services.save_tracking_code(order, form.cleaned_data["tracking_code"])
            messages.success(request, "Tracking code saved on CardTrader.")
        else:
            if not order.can_ship:
                messages.error(request, f"Order #{order.code} is {order.state_label.lower()}: it cannot be shipped.")
                return redirect("ledger:order_detail", pk=pk)
            services.ship_order(order, form.cleaned_data["tracking_code"])
            messages.success(request, f"Order #{order.code} marked as shipped.")
            return redirect("ledger:shipments")
    except CardTraderError as exc:
        messages.error(request, str(exc))
    return redirect("ledger:order_detail", pk=pk)


@login_required
@require_POST
def orders_sync(request):
    service = services.get_cardtrader()
    try:
        with services.make_client(service) as client:
            created, updated = services.import_orders(client, service)
    except CardTraderError as exc:
        messages.error(request, f"Orders not imported: {exc}")
    else:
        messages.success(request, f"Orders imported: {created} new, {updated} updated.")
    return redirect("ledger:shipments")


@login_required
@ensure_csrf_cookie
def scan_app(request, rest=""):
    """The React scan app: every /scan/... URL serves the built SPA's index.html; the app routes on the client."""
    index = settings.SCAN_APP_DIR / "index.html"
    try:
        html = index.read_bytes()
    except OSError:
        return HttpResponse("Scan app not built: set SCAN_APP_DIR (see README)", status=503,
                            content_type="text/plain; charset=utf-8")
    return HttpResponse(html, content_type="text/html; charset=utf-8")
