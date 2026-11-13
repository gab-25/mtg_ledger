"""The sale API (/api/listings/): CardTrader listings, their edit form, publishing and market prices."""

from django.db.models import DecimalField, ExpressionWrapper, F, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from .. import cardmarket, services
from ..cardtrader import CardTraderError
from ..forms import ListingForm
from ..models import CollectionItem, Listing
from ..presentation import listing_badge
from .common import doc, form_body, form_errors, messages_response, msg, query
from .errors import ServiceError

TAGS = ["sale"]
VALUE = ExpressionWrapper(F("price") * F("quantity"), output_field=DecimalField())


def _money(value):
    return None if value is None else f"{value:.2f}"


def _row(listing):
    item = listing.item
    return {
        "item_id": item.pk,
        "name": item.card.name,
        "set_code": item.card.set_code,
        "condition": item.condition,
        "language": item.language,
        "foil": item.foil,
        "quantity": listing.quantity,
        "price": _money(listing.price),
        "badge": listing_badge(listing.status),
    }


@doc(TAGS, "Listings with the sale stats (?status filters them)",
     parameters=[query("status", "Only the listings in this status", enum=Listing.Status.values)])
@api_view(["GET"])
def listings(request):
    qs = Listing.objects.owned_by(request.user).select_related("item__card")
    active = qs.exclude(status=Listing.Status.SOLD)
    month_start = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    stats = {
        "active": active.count(),
        "value": _money(active.aggregate(v=Sum(VALUE))["v"] or 0),
        "sold_month": qs.filter(status=Listing.Status.SOLD, sold_at__gte=month_start).count(),
        "to_publish": qs.filter(status=Listing.Status.TO_PUBLISH).count(),
    }
    status = request.query_params.get("status", "")
    if status in Listing.Status.values:
        qs = qs.filter(status=status)
    else:
        status = ""
    return Response({"stats": stats, "status": status, "results": [_row(l) for l in qs]})


@doc(TAGS, "The listing of a collection item: read (GET), create or change (PUT), remove (DELETE)",
     request=form_body("Listing", ListingForm))
@extend_schema(methods=["GET"], operation_id="listing_retrieve")
@api_view(["GET", "PUT", "DELETE"])
def listing(request, item_pk):
    item = get_object_or_404(CollectionItem.objects.owned_by(request.user).select_related("card"), pk=item_pk)
    existing = getattr(item, "listing", None)

    if request.method == "DELETE":
        if existing is None:
            raise NotFound()
        try:
            services.remove_listing(existing)
        except CardTraderError as exc:
            raise ServiceError(f"Listing not removed: {exc}")
        return messages_response(msg("success", "Listing removed."))

    obj = existing or Listing(item=item, price=cardmarket.suggested_price(item.card, item.foil), quantity=1)
    if request.method == "PUT":
        old_status = obj.status
        form = ListingForm(request.data, instance=obj)
        if not form.is_valid():
            raise form_errors(form)
        obj = form.save(commit=False)
        if obj.status == Listing.Status.SOLD and old_status != Listing.Status.SOLD:
            obj.sold_at = timezone.now()
        elif (
            obj.pk
            and obj.status == Listing.Status.PUBLISHED
            and "status" not in form.changed_data
            and form.changed_data
        ):
            # Edited locally: CardTrader needs the new values.
            obj.status = Listing.Status.TO_PUBLISH
        obj.save()
        return messages_response(msg("success", f"Listing for {item.card.name} saved."))

    return Response({
        "item": {
            "id": item.pk, "name": item.card.name, "set_code": item.card.set_code, "condition": item.condition,
            "language": item.language, "foil": item.foil, "quantity": item.quantity,
        },
        "listing": {
            "exists": obj.pk is not None, "quantity": obj.quantity, "price": _money(obj.price),
            "status": obj.status,
        },
        "cm_suggestion": _money(cardmarket.suggested_price(item.card, item.foil)),
    })


@doc(TAGS, "Publish the listings waiting to be published to CardTrader")
@api_view(["POST"])
def publish(request):
    try:
        result = services.publish_pending(request.user)
    except CardTraderError as exc:
        raise ServiceError(f"Publish failed: {exc}")
    n = result.published
    msgs = [msg("success", f"Published {n} listing{'s' if n != 1 else ''} to CardTrader.")]
    msgs += [msg("warning", f"{l.item.card.name}: {reason}") for l, reason in result.failed]
    return messages_response(*msgs)
