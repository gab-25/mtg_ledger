"""Camera scan sessions: the draft list of scanned cards and saving it into the collection."""

from django.db import IntegrityError, transaction
from django.db.models import Q

from . import catalog, pricing
from .cardtrader import CardTraderError
from .models import CollectionItem, Listing, ScanCapture, ScanLine, ScanSession, SyncLog

UNSET = object()


class ScanError(Exception):
    pass


def start_session(owner, expansion, condition):
    """New session for owner; the sale threshold is remembered from their latest session."""
    last = ScanSession.objects.owned_by(owner).order_by("-created_at", "-id").first()
    return ScanSession.objects.create(
        owner=owner, expansion=expansion, default_condition=condition,
        sale_threshold=last.sale_threshold if last else None,
    )


def _price(client, line):
    if line.blueprint is None or not line.language:
        return None
    if client is None:
        from . import services

        try:
            with services.service_client() as own:
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
        ScanCapture.objects.filter(line=line).update(line=twin)
        line.delete()
    return twin


def _add_certain(session, client, blueprint, language, foil):
    line = ScanLine(
        session=session, blueprint=blueprint, language=language, foil=foil, condition=session.default_condition,
    )
    twin = _twin(line)
    if twin is not None:
        twin.quantity += 1
        twin.save(update_fields=["quantity", "updated_at"])
        return twin, True
    line.price = _price(client, line)
    line.save()
    return line, False


def reserve_capture(session, capture_id):
    """Claim a capture id. Returns (capture, created); created=False means it was seen before."""
    try:
        with transaction.atomic():
            return ScanCapture.objects.create(session=session, capture_id=capture_id), True
    except IntegrityError:
        return ScanCapture.objects.select_related("line").get(session=session, capture_id=capture_id), False


def release_capture(capture):
    """Forget a capture that failed, so the phone can retry it with the same id."""
    if capture is not None and not capture.done:
        capture.delete()


def add_capture(session, client, rec, m, *, frame, capture_id="", capture=None):
    """Add what one camera capture recognised. Returns (line, merged); a repeated capture id changes nothing."""
    if capture is None and capture_id:
        capture, created = reserve_capture(session, capture_id)
        if not created:
            return capture.line, False
    if m.blueprint is not None:
        line, merged = _add_certain(session, client, m.blueprint, rec.language, rec.foil)
    else:
        line = ScanLine.objects.create(
            session=session, language=rec.language, foil=rec.foil, condition=session.default_condition,
            needs_confirmation=True, candidates=[b.ct_id for b in m.candidates], frame=frame,
            read_name=rec.name.strip()[:200],
        )
        merged = False
    if capture is not None:
        capture.line, capture.done = line, True
        capture.save(update_fields=["line", "done"])
    return line, merged


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
    return catalog.card_for_blueprint(line.blueprint)


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
                owner=session.owner, card=card, condition=line.condition, language=line.language, foil=line.foil,
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
        SyncLog.objects.create(owner=session.owner, message=f"Camera scan: {saved} cards saved, {listed} queued for sale")
    original.status = session.status
    return saved, listed
