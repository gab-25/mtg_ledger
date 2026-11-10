"""The orders API (/api/orders/): CardTrader sale orders, their packing list, shipping and import."""

from django.db.models import Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.decorators import api_view
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from .. import services
from ..cardtrader import CardTraderError
from ..forms import ShipForm
from ..models import Order
from ..presentation import ORDER_TABS, order_badge
from .common import doc, form_body, messages_response, msg, query
from .errors import Blocked, ServiceError

TAGS = ["orders"]
ROW_LIMIT = 200


def _money(value):
    return None if value is None else f"{value:.2f}"


def _row(order):
    return {
        "id": order.pk,
        "code": order.code,
        "packing_number": order.packing_number,
        "can_ship": order.can_ship,
        "buyer_username": order.buyer_username,
        "via_cardtrader_zero": order.via_cardtrader_zero,
        "ship_city": order.ship_city,
        "ship_country_code": order.ship_country_code,
        "shipping_method": order.shipping_method,
        "tracked": order.tracked,
        "size": order.size,
        "sent_at": order.sent_at,
        "paid_at": order.paid_at,
        "total": _money(order.total),
        "badge": order_badge(order.state),
    }


@doc(TAGS, "Orders of one tab (?tab: to_ship, ct_zero, shipped, completed, issues) with the shipping stats",
     parameters=[query("tab", "The tab (default: to_ship)", enum=list(ORDER_TABS))])
@api_view(["GET"])
def orders(request):
    tab = request.query_params.get("tab", "to_ship")
    if tab not in ORDER_TABS:
        tab = "to_ship"
    qs = Order.objects.owned_by(request.user).prefetch_related("items")
    month_start = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    to_ship = qs.filter(state=Order.State.PAID)
    stats = {
        "to_ship": to_ship.count(),
        "items_to_ship": to_ship.aggregate(n=Sum("size"))["n"] or 0,
        "shipped_month": qs.filter(sent_at__gte=month_start).count(),
        "revenue_month": _money(
            qs.filter(paid_at__gte=month_start).exclude(state__in=[Order.State.CANCELED, Order.State.CLOSED])
            .aggregate(v=Sum("total"))["v"] or 0
        ),
    }
    tabs = [{"key": key, "label": label, "count": qs.filter(state__in=states).count()}
            for key, (label, states) in ORDER_TABS.items()]
    shown = qs.filter(state__in=ORDER_TABS[tab][1])
    if tab == "to_ship":
        shown = shown.order_by("packing_number", "paid_at")
    return Response({
        "tab": tab,
        "stats": stats,
        "tabs": tabs,
        "orders_synced_at": services.get_cardtrader(request.user).orders_synced_at,
        "results": [_row(o) for o in shown[:ROW_LIMIT]],
    })


@doc(TAGS, "One order with its address and packing list")
@extend_schema(operation_id="order_retrieve")
@api_view(["GET"])
def order(request, pk):
    obj = get_object_or_404(Order.objects.owned_by(request.user).prefetch_related("items__listing__item"), pk=pk)
    return Response({
        **_row(obj),
        "state": obj.state,
        "state_label": obj.state_label,
        "address_lines": obj.address_lines,
        "ship_note": obj.ship_note,
        "buyer_email": obj.buyer_email,
        "buyer_phone": obj.buyer_phone,
        "max_shipping_days": obj.max_shipping_days,
        "tracking_code": obj.tracking_code,
        "items": [
            {
                "id": it.pk, "name": it.name, "expansion": it.expansion, "foil": it.foil,
                "condition": it.condition, "language": it.language, "quantity": it.quantity,
                "price": _money(it.price), "item_pk": it.listing.item_id if it.listing else None,
            }
            for it in obj.items.all()
        ],
    })


@doc(TAGS, "Save the tracking code (save_tracking: true) or mark the order as shipped",
     request=form_body("Ship", ShipForm, save_tracking=serializers.BooleanField(
         required=False, help_text="true: only save the tracking code on CardTrader, without shipping")))
@api_view(["POST"])
def ship(request, pk):
    obj = get_object_or_404(Order.objects.owned_by(request.user), pk=pk)
    form = ShipForm({"tracking_code": request.data.get("tracking_code") or ""})
    if not form.is_valid():
        raise ValidationError({"detail": "Invalid tracking code.", "code": "invalid"})
    tracking_code = form.cleaned_data["tracking_code"]
    try:
        if request.data.get("save_tracking"):
            services.save_tracking_code(obj, tracking_code)
            return messages_response(msg("success", "Tracking code saved on CardTrader."), shipped=False)
        if not obj.can_ship:
            raise Blocked(f"Order #{obj.code} is {obj.state_label.lower()}: it cannot be shipped.")
        services.ship_order(obj, tracking_code)
    except CardTraderError as exc:
        raise ServiceError(str(exc))
    return messages_response(msg("success", f"Order #{obj.code} marked as shipped."), shipped=True)


@doc(TAGS, "Import new and changed orders from CardTrader")
@api_view(["POST"])
def import_orders(request):
    service = services.get_cardtrader(request.user)
    try:
        with services.user_client(request.user) as client:
            created, updated = services.import_orders(client, service)
    except CardTraderError as exc:
        raise ServiceError(f"Orders not imported: {exc}")
    return messages_response(msg("success", f"Orders imported: {created} new, {updated} updated."))
