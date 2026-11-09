"""The collection API (/api/collection/): list with filters and stats, add/edit/remove cards, CSV import and export."""

from django.db.models import DecimalField, ExpressionWrapper, F, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

from .. import services
from ..forms import CardItemForm, CsvImportForm
from ..models import Card, CollectionItem, Color, Condition, Language, Listing, SyncLog
from ..presentation import COLOR_FILTERS, item_badge, pip
from .common import doc, form_errors, messages_response, msg, page_payload

TAGS = ["collection"]
VALUE = ExpressionWrapper(F("price") * F("quantity"), output_field=DecimalField())


def _choices(choices):
    return [[value, label] for value, label in choices]


@doc(TAGS, "Choices of the card and listing forms: colors, conditions, languages, listing statuses")
@api_view(["GET"])
def choices(request):
    return Response({
        "colors": _choices(Color.choices),
        "conditions": _choices(Condition.choices),
        "languages": _choices(Language.choices),
        "listing_statuses": _choices(Listing.Status.choices),
    })


def _row(item):
    card = item.card
    return {
        "id": item.pk,
        "name": card.name,
        "type_line": card.type_line,
        "set_code": card.set_code,
        "foil": item.foil,
        "condition": item.condition,
        "condition_label": item.get_condition_display(),
        "language": item.language,
        "quantity": item.quantity,
        "price": f"{item.price:.2f}",
        "pip": pip(card.color),
        "badge": item_badge(item.status),
    }


def _collection(request):
    user = request.user
    items = CollectionItem.objects.owned_by(user).select_related("card", "listing")
    stats = items.aggregate(total=Sum("quantity"), value=Sum(VALUE))
    stats["unique"] = Card.objects.filter(items__owner=user).distinct().count()
    stats["for_sale"] = (
        Listing.objects.owned_by(user).exclude(status=Listing.Status.SOLD).aggregate(n=Sum("quantity"))["n"] or 0
    )
    if stats["value"] is not None:
        stats["value"] = f"{stats['value']:.2f}"

    params = request.query_params
    q = params.get("q", "").strip()
    color = params.get("color", "")
    foil = params.get("foil") == "1"
    set_code = params.get("set", "")
    cond = params.get("cond", "")
    if q:
        items = items.filter(Q(card__name__icontains=q) | Q(card__set_code__iexact=q) | Q(card__type_line__icontains=q))
    if color in Color.values:
        items = items.filter(card__color=color)
    if foil:
        items = items.filter(foil=True)
    if set_code:
        items = items.filter(card__set_code=set_code)
    if cond in Condition.values:
        items = items.filter(condition=cond)

    return Response({
        **page_payload(request, items, _row),
        "stats": stats,
        "sets": list(Card.objects.filter(items__owner=user).values_list("set_code", flat=True)
                     .distinct().order_by("set_code")),
        "color_filters": [{"code": code, "label": label, "pip": pip(code)} for code, label in COLOR_FILTERS],
        "filtered": bool(q or color or foil or set_code or cond),
    })


@doc(TAGS, "The collection (GET: ?q, color, foil, set, cond, page) or add cards to it (POST)")
@api_view(["GET", "POST"])
def collection(request):
    if request.method == "GET":
        return _collection(request)
    form = CardItemForm(request.data, owner=request.user)
    if not form.is_valid():
        raise form_errors(form)
    item = form.save()
    return messages_response(msg("success", f"Added {item.quantity}× {item.card.name}."),
                             status=status.HTTP_201_CREATED, id=item.pk)


@doc(TAGS, "One collection item: read (GET), change (PUT) or remove (DELETE)")
@extend_schema(methods=["GET"], operation_id="collection_item_retrieve")
@api_view(["GET", "PUT", "DELETE"])
def item(request, pk):
    obj = get_object_or_404(CollectionItem.objects.owned_by(request.user).select_related("card"), pk=pk)
    if request.method == "DELETE":
        name = obj.card.name
        obj.delete()
        return messages_response(msg("success", f"Removed {name} from the collection."))
    if request.method == "PUT":
        form = CardItemForm(request.data, instance=obj)
        if not form.is_valid():
            raise form_errors(form)
        form.save()
        return messages_response(msg("success", f"Saved {obj.card.name}."))
    card = obj.card
    return Response({
        "id": obj.pk,
        "name": card.name,
        "set_code": card.set_code,
        "collector_number": card.collector_number,
        "type_line": card.type_line,
        "color": card.color,
        "condition": obj.condition,
        "language": obj.language,
        "foil": obj.foil,
        "quantity": obj.quantity,
        "price": f"{obj.price:.2f}",
        "has_listing": hasattr(obj, "listing"),
    })


@doc(TAGS, "CSV import: the columns (GET) or import a file (POST multipart 'file')")
@api_view(["GET", "POST"])
@parser_classes([MultiPartParser])
def import_csv(request):
    if request.method == "GET":
        return Response({"columns": list(services.CSV_FIELDS)})
    form = CsvImportForm(data={}, files=request.FILES)
    if form.is_valid():
        try:
            created, updated = services.import_csv(form.cleaned_data["file"], request.user)
        except services.CsvImportError as exc:
            form.add_error("file", str(exc))
        else:
            return messages_response(msg("success", f"Import complete: {created} new, {updated} updated."))
    raise form_errors(form)


@doc(TAGS, "Download the collection as CSV")
@api_view(["GET"])
def export_csv(request):
    items = CollectionItem.objects.owned_by(request.user)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    stamp = timezone.localdate().isoformat()
    response["Content-Disposition"] = f'attachment; filename="mtg-ledger-{stamp}.csv"'
    services.export_csv(items, response)
    SyncLog.objects.create(owner=request.user, message=f"CSV export: collection exported ({items.count():,} rows)")
    return response
