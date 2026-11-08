from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import services
from .forms import CardItemForm, CsvImportForm, ListingForm, SyncServiceForm
from .models import Card, CollectionItem, Color, Condition, Listing, SyncConflict, SyncLog, SyncService
from .presentation import COLOR_FILTERS, item_badge, listing_badge, pip

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
    return render(request, "ledger/listing_form.html", {"nav": "sale", "form": form, "item": item, "listing": listing})


@login_required
@require_POST
def listing_delete(request, item_pk):
    listing = get_object_or_404(Listing, item__pk=item_pk)
    listing.delete()
    messages.success(request, "Listing removed.")
    return redirect("ledger:sale")


@login_required
@require_POST
def publish(request):
    count = services.publish_pending()
    messages.success(request, f"Published {count} listing{'s' if count != 1 else ''} to CardTrader.")
    return redirect(request.POST.get("next") or "ledger:sale")


@login_required
@require_POST
def match_prices(request):
    count = services.match_market_prices()
    messages.success(request, f"{count} prices matched to market." if count else "All prices already match the market.")
    return redirect("ledger:sale")


# --- Sync ----------------------------------------------------------------

@login_required
def sync(request):
    services.get_cardtrader()
    connections = SyncService.objects.annotate(
        open_conflicts=Count("conflicts", filter=Q(conflicts__resolution=SyncConflict.Resolution.OPEN))
    )
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
    services.sync_all()
    messages.success(request, "Sync complete.")
    return redirect("ledger:sync")


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


@login_required
def service_settings(request, pk):
    service = get_object_or_404(SyncService, pk=pk)
    form = SyncServiceForm(request.POST or None, instance=service)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"{service.name} settings saved.")
        return redirect("ledger:sync")
    return render(request, "ledger/service_form.html", {"nav": "sync", "form": form, "service": service})
