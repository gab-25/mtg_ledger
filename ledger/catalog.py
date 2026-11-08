"""Local copy of the CardTrader catalog and printing lookup."""

import re

_NUMBER = re.compile(r"^0*(\d+)(.*)$")


def normalize_number(value):
    """Canonical collector number: no spaces, no leading zeros, upper case ("012a" -> "12A")."""
    value = (value or "").strip().replace(" ", "").upper()
    m = _NUMBER.match(value)
    if m:
        return (m.group(1) or "0") + m.group(2)
    return value


def blueprint_number(bp):
    """Collector number of a CardTrader blueprint (export payload), normalised."""
    return normalize_number((bp.get("fixed_properties") or {}).get("collector_number", ""))


def merge_cards(source, target):
    """Move source's copies onto target (same printing). Returns False, changing nothing, on a listing clash."""
    from django.db import transaction

    from .models import Listing

    def live_listing(item):
        listing = getattr(item, "listing", None)
        return listing if listing is not None and listing.status != Listing.Status.SOLD else None

    pairs = []
    for item in source.items.select_related("listing"):
        twin = target.items.select_related("listing").filter(
            condition=item.condition, language=item.language, foil=item.foil
        ).first()
        if twin is not None and live_listing(item) and live_listing(twin):
            return False
        pairs.append((item, twin))

    with transaction.atomic():
        for item, twin in pairs:
            if twin is None:
                item.card = target
                item.save(update_fields=["card", "updated_at"])
                continue
            twin.quantity += item.quantity
            twin.save(update_fields=["quantity", "updated_at"])
            listing = getattr(item, "listing", None)
            if listing is not None and live_listing(twin) is None:
                old = getattr(twin, "listing", None)  # a sold listing on the twin gives way
                if old is not None:
                    old.delete()
                listing.item = twin
                listing.save(update_fields=["item", "updated_at"])
            item.delete()
        source.delete()
    return True
