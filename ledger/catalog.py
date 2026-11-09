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


def _live_listing(item):
    from .models import Listing

    listing = getattr(item, "listing", None)
    return listing if listing is not None and listing.status != Listing.Status.SOLD else None


def _merge_pairs(source, target):
    """[(source item, same owner's twin on target or None)]: copies only ever fold into their owner's own."""
    pairs = []
    for item in source.items.select_related("listing"):
        twin = target.items.select_related("listing").filter(
            owner_id=item.owner_id, condition=item.condition, language=item.language, foil=item.foil
        ).first()
        pairs.append((item, twin))
    return pairs


def clashing_owners(source, target):
    """The users (ids) whose copies on both cards have live listings: merging would need two listings in one."""
    return {
        item.owner_id for item, twin in _merge_pairs(source, target)
        if twin is not None and _live_listing(item) and _live_listing(twin)
    }


def merge_cards(source, target):
    """Move source's copies onto target (same printing). Returns False, changing nothing, on a listing clash."""
    from django.db import transaction

    if clashing_owners(source, target):
        return False
    pairs = _merge_pairs(source, target)
    live_listing = _live_listing

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


def _blueprint_fields(bp):
    """Map a /blueprints/export entry to Blueprint fields (shape to re-check against the live API)."""
    fixed = bp.get("fixed_properties") or {}
    image = bp.get("image_url") or ((bp.get("image") or {}).get("url") if isinstance(bp.get("image"), dict) else "")
    return {
        "name": bp["name"],
        "collector_number": blueprint_number(bp),
        "version": bp.get("version") or "",
        "rarity": fixed.get("mtg_rarity") or "",
        "image_url": (image or "")[:500],
        "cm_price_id": next(iter(bp.get("card_market_ids") or []), None),
    }


def sync_expansions(client):
    """Upsert every MTG expansion from CardTrader. Returns how many there are."""
    from .cardtrader import MTG_GAME_ID
    from .models import Expansion

    count = 0
    for e in client.expansions():
        if e.get("game_id") != MTG_GAME_ID or not e.get("code"):
            continue
        Expansion.objects.update_or_create(ct_id=e["id"], defaults={"code": e["code"].lower(), "name": e["name"]})
        count += 1
    return count


def ensure_blueprints(client, expansion, *, force=False):
    """Load an expansion's blueprints once (or again with force). Returns how many were stored."""
    from django.utils import timezone

    from .models import Blueprint

    if expansion.blueprints_synced_at and not force:
        return 0
    count = 0
    for bp in client.blueprints(expansion.ct_id):
        Blueprint.objects.update_or_create(
            ct_id=bp["id"], defaults={"expansion": expansion, **_blueprint_fields(bp)}
        )
        count += 1
    expansion.blueprints_synced_at = timezone.now()
    expansion.save(update_fields=["blueprints_synced_at"])
    return count


def expansion_by_code(code):
    from .models import Expansion

    code = (code or "").strip().lower()
    return Expansion.objects.filter(code=code).first() if code else None
