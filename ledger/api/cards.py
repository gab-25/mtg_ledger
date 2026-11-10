"""The card catalog API (/api/cards/): search CardTrader printings with Cardmarket prices and owned copies."""

from django.db.models import Max, Q
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .. import cardmarket
from ..catalog import normalize_number
from ..models import Blueprint, CardmarketPrice
from .common import PAGE, doc, page_payload, query

TAGS = ["cards"]
PRICE_FIELDS = ("trend", "low", "avg", "trend_foil", "low_foil", "avg_foil")


def _money(value):
    return None if value is None else f"{value:.2f}"


@doc(TAGS, "Search the card database by ?q (name or set code), set, number, owned=1; page", parameters=[
    query("q", "Name, or exact set code"),
    query("set", "Exact set code"),
    query("number", "Collector number"),
    query("owned", "1: only the printings you own", enum=["1"]),
    PAGE,
])
@api_view(["GET"])
def cards(request):
    params = request.query_params
    q = params.get("q", "").strip()
    set_code = params.get("set", "").strip().lower()
    number = normalize_number(params.get("number", ""))
    owned = params.get("owned") == "1"
    filters = {"q": q, "set": set_code, "number": number, "owned": owned}
    if not (q or set_code or number or owned):
        return Response({"searched": False, "filters": filters})

    owned_qty = cardmarket.owned_quantities(request.user)
    printings = Blueprint.objects.select_related("expansion", "cm_price").order_by("name", "expansion__code")
    if q:
        printings = printings.filter(Q(name__icontains=q) | Q(expansion__code__iexact=q))
    if set_code:
        printings = printings.filter(expansion__code=set_code)
    if number:
        printings = printings.filter(collector_number=number)
    if owned:
        printings = printings.filter(ct_id__in=owned_qty)

    def printing(bp):
        price = bp.cm_price
        return {
            "ct_id": bp.ct_id,
            "name": bp.name,
            "version": bp.version,
            "image_url": bp.image_url,
            "expansion_code": bp.expansion.code,
            "expansion_name": bp.expansion.name,
            "collector_number": bp.collector_number,
            "owned_qty": owned_qty.get(bp.ct_id),
            "prices": None if price is None else {f: _money(getattr(price, f)) for f in PRICE_FIELDS},
        }

    return Response({
        "searched": True,
        "filters": filters,
        **page_payload(request, printings, printing),
        "prices_date": CardmarketPrice.objects.aggregate(d=Max("prices_date"))["d"],
    })
