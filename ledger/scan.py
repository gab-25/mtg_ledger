"""Camera scan sessions: the draft list of scanned cards and saving it into the collection."""

from django.db import transaction
from django.db.models import Q

from . import pricing
from .cardtrader import CardTraderError
from .forms import get_or_create_card
from .models import Card, CollectionItem, Color, Listing, ScanLine, ScanSession, SyncLog

UNSET = object()


class ScanError(Exception):
    pass


def start_session(expansion, condition):
    """New session; the sale threshold is remembered from the latest session."""
    last = ScanSession.objects.order_by("-created_at", "-id").first()
    return ScanSession.objects.create(
        expansion=expansion, default_condition=condition,
        sale_threshold=last.sale_threshold if last else None,
    )


def _price(client, line):
    if line.blueprint is None or not line.language:
        return None
    if client is None:
        from . import services

        try:
            with services.make_client() as own:
                return pricing.price_for(own, line.blueprint.ct_id, line.language, line.foil, line.condition)
        except CardTraderError:
            return None
    return pricing.price_for(client, line.blueprint.ct_id, line.language, line.foil, line.condition)


def _twin(line):
    """Another confirmed line of the session with the same printing/language/foil/condition."""
    return (
        line.session.lines.filter(
            needs_confirmation=False, blueprint=line.blueprint, language=line.language,
            foil=line.foil, condition=line.condition,
        )
        .exclude(pk=line.pk)
        .first()
    )


def _merge_or_save(line):
    """Fold a confirmed line into its twin if there is one. Returns the surviving line."""
    twin = _twin(line) if not line.needs_confirmation else None
    if twin is None:
        line.save()
        return line
    twin.quantity += line.quantity
    twin.save(update_fields=["quantity", "updated_at"])
    if line.pk:
        line.delete()
    return twin


def _add_certain(session, client, blueprint, language, foil, *, capture_id="", type_line="", color=""):
    line = ScanLine(
        session=session, blueprint=blueprint, language=language, foil=foil,
        condition=session.default_condition, capture_id=capture_id, type_line=type_line, color=color,
    )
    twin = _twin(line)
    if twin is not None:
        twin.quantity += 1
        twin.save(update_fields=["quantity", "updated_at"])
        return twin, True
    line.price = _price(client, line)
    line.save()
    return line, False


def add_capture(session, client, rec, m, *, frame, capture_id):
    """Add what one camera capture recognised. Returns (line, merged)."""
    if capture_id:
        done = session.lines.filter(capture_id=capture_id).first()
        if done is not None:
            return done, False
    if m.blueprint is not None:
        return _add_certain(
            session, client, m.blueprint, rec.language, rec.foil,
            capture_id=capture_id, type_line=rec.type_line, color=rec.color,
        )
    line = ScanLine.objects.create(
        session=session, language=rec.language, foil=rec.foil, condition=session.default_condition,
        needs_confirmation=True, candidates=[b.ct_id for b in m.candidates], frame=frame,
        capture_id=capture_id, type_line=rec.type_line, color=rec.color,
    )
    return line, False


def add_manual(session, client, blueprint, language, foil):
    """The Search button: add one copy of a printing picked by hand."""
    line, _ = _add_certain(session, client, blueprint, language, foil)
    return line


def undo(line):
    if line.quantity > 1:
        line.quantity -= 1
        line.save(update_fields=["quantity", "updated_at"])
    else:
        line.delete()


def update_line(line, *, client=None, foil=None, condition=None, language=None, quantity=None, for_sale=UNSET):
    """Edit a draft line from the toast or the review screen. Returns the surviving line (None if deleted)."""
    if quantity is not None and quantity <= 0:
        line.delete()
        return None
    key = (line.language, line.foil, line.condition)
    if foil is not None:
        line.foil = foil
    if condition is not None:
        line.condition = condition
    if language is not None:
        line.language = language
    if quantity is not None:
        line.quantity = quantity
    if for_sale is not UNSET:
        line.for_sale = for_sale
    if (line.language, line.foil, line.condition) != key and not line.needs_confirmation:
        line.price = _price(client, line)
        return _merge_or_save(line)
    line.save()
    return line


def confirm_line(line, client, blueprint, language):
    """Resolve an uncertain line with the printing and language the owner picked."""
    line.blueprint = blueprint
    line.language = language
    line.needs_confirmation = False
    line.frame = None
    line.candidates = []
    line.price = _price(client, line)
    return _merge_or_save(line)


def _card_for(line):
    bp = line.blueprint
    card = Card.objects.filter(ct_blueprint_id=bp.ct_id).first()
    if card is not None:
        return card
    set_code = bp.expansion.code.upper()
    defaults = {"type_line": line.type_line, "color": line.color or Color.COLORLESS}
    if not bp.version:  # base printing: adopt a hand-entered (name + set) row instead of duplicating it
        legacy = Card.objects.filter(
            name__iexact=bp.name, set_code=set_code, collector_number="", ct_blueprint_id__isnull=True
        ).first()
        if legacy is not None and not Card.objects.filter(
            ~Q(collector_number=""), set_code=set_code, collector_number=bp.collector_number
        ).exists():
            legacy.ct_blueprint_id = bp.ct_id
            legacy.collector_number = bp.collector_number
            legacy.save(update_fields=["ct_blueprint_id", "collector_number"])
            return legacy
    card = get_or_create_card(bp.name, set_code, bp.collector_number, **defaults)
    if card.ct_blueprint_id is None:
        card.ct_blueprint_id = bp.ct_id
        card.save(update_fields=["ct_blueprint_id"])
    return card


def _list(item, line):
    listing = getattr(item, "listing", None)
    if listing is None:
        Listing.objects.create(item=item, quantity=line.quantity, price=line.price)
    elif listing.status == Listing.Status.SOLD:
        listing.quantity, listing.price, listing.sold_at = line.quantity, line.price, None
        listing.status = Listing.Status.TO_PUBLISH
        listing.save()
    else:
        listing.quantity += line.quantity
        listing.status = Listing.Status.TO_PUBLISH
        listing.save(update_fields=["quantity", "status", "updated_at"])


def save_session(session):
    """Write the draft into the collection. Returns (copies_saved, copies_listed)."""
    original = session
    with transaction.atomic():
        session = ScanSession.objects.select_for_update().get(pk=session.pk)
        if session.status == ScanSession.Status.SAVED:
            raise ScanError("This scan session was already saved.")
        if session.lines.filter(needs_confirmation=True).exists():
            raise ScanError("Some cards still need confirmation.")
        saved = listed = 0
        for line in session.lines.select_related("blueprint__expansion"):
            card = _card_for(line)
            item, created = CollectionItem.objects.get_or_create(
                card=card, condition=line.condition, language=line.language, foil=line.foil,
                defaults={"quantity": line.quantity, "price": line.price or 0},
            )
            if not created:
                item.quantity += line.quantity  # the existing unit price is kept
                item.save(update_fields=["quantity", "updated_at"])
            saved += line.quantity
            if line.listed:
                _list(item, line)
                listed += line.quantity
        session.status = ScanSession.Status.SAVED
        session.save(update_fields=["status", "updated_at"])
        SyncLog.objects.create(message=f"Camera scan: {saved} cards saved, {listed} queued for sale")
    original.status = session.status
    return saved, listed
