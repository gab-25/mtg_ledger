"""Market prices: cheapest CardTrader offers per printing, language, foil and condition.

The rows are the last known prices (no expiry on read): the nightly catalog sync refreshes every combination someone
owns, and adding a card from the catalog fills a missing one.
"""

import time
from datetime import timedelta

from django.db import transaction
from django.db.models import Min, OuterRef, Subquery
from django.utils import timezone

from .cardtrader import CONDITION_FROM_CT, LANGUAGE_TO_CT, CardTraderError, money
from .models import Condition, MarketPrice, SyncLog

PRICE_TTL = timedelta(hours=6)
PRODUCT_CONDITION = Condition.NEAR_MINT  # products have no condition on CardTrader
LANGUAGE_FROM_CT = {v: k for k, v in LANGUAGE_TO_CT.items()}


def _cheapest(offers, language=None, foil=None):
    """{(language, foil, condition): cheapest price} from marketplace offers, whoever sells them.

    The cache is shared by every user, so nobody's own offers are left out.

    language/foil, when given, are the filter the request already applied (offers may not repeat them).
    """
    best = {}
    for p in offers:
        props = p.get("properties_hash") or {}
        lang = language or LANGUAGE_FROM_CT.get(props.get("mtg_language"))
        cond = CONDITION_FROM_CT.get(props.get("condition"))
        price = money(p.get("price"))
        if lang is None or cond is None or price is None:
            continue
        key = (lang, bool(props.get("mtg_foil")) if foil is None else foil, cond)
        if key not in best or price < best[key]:
            best[key] = price
    return best


def _store(blueprint_ct_id, best):
    now = timezone.now()
    for (language, foil, condition), price in best.items():
        MarketPrice.objects.update_or_create(
            blueprint_ct_id=blueprint_ct_id, language=language, foil=foil, condition=condition,
            defaults={"price": price, "fetched_at": now},
        )


def refresh(client, blueprint_ct_id, language, foil, *, is_product=False):
    """Replace the stored prices of one (printing, language, foil) with CardTrader's current cheapest offers.

    Cards: one call filtered by language and foil, the cheapest offer per condition. Products have no condition or
    language on CardTrader: the cheapest offer of the blueprint, stored as Near Mint, non-foil, in language.
    Returns the rows stored; raises CardTraderError.
    """
    if is_product:
        offers = client.marketplace_products(blueprint_ct_id)
        prices = [p for p in (money(o.get("price")) for o in offers) if p is not None]
        best = {(language, False, PRODUCT_CONDITION): min(prices)} if prices else {}
        foil = False
    else:
        offers = client.marketplace_products(blueprint_ct_id, language=LANGUAGE_TO_CT[language], foil=foil)
        best = _cheapest(offers, language=language, foil=foil)
    with transaction.atomic():
        MarketPrice.objects.filter(blueprint_ct_id=blueprint_ct_id, language=language, foil=foil).delete()
        _store(blueprint_ct_id, best)
    return len(best)


def price(blueprint_ct_id, language, foil, condition):
    """The stored cheapest offer of exactly this combination, or None."""
    row = MarketPrice.objects.filter(
        blueprint_ct_id=blueprint_ct_id, language=language, foil=foil, condition=condition,
    ).first()
    return row.price if row is not None else None


def price_subquery(prefix=""):
    """The stored price of a CollectionItem's exact combination, for annotating a CollectionItem queryset."""
    return Subquery(MarketPrice.objects.filter(
        blueprint_ct_id=OuterRef(f"{prefix}card__ct_blueprint_id"), language=OuterRef(f"{prefix}language"),
        foil=OuterRef(f"{prefix}foil"), condition=OuterRef(f"{prefix}condition"),
    ).values("price")[:1])


def ensure(client, item):
    """Fetch the prices of a collection item's combination if none are stored. Errors go to the SyncLog."""
    card = item.card
    if card.ct_blueprint_id is None or MarketPrice.objects.filter(
        blueprint_ct_id=card.ct_blueprint_id, language=item.language, foil=item.foil and not card.is_product,
    ).exists():
        return
    try:
        refresh(client, card.ct_blueprint_id, item.language, item.foil, is_product=card.is_product)
    except CardTraderError as exc:
        SyncLog.objects.create(level=SyncLog.Level.WARNING, message=f"Market price unavailable: {exc}"[:300])


def fill_expansion(client, expansion):
    """Cache the cheapest offers of every printing in an expansion with one call. Returns rows stored."""
    stored = 0
    for bp_id, offers in client.marketplace_by_expansion(expansion.ct_id).items():
        best = _cheapest(offers)
        _store(int(bp_id), best)
        stored += len(best)
    return stored


def _lookup(blueprint_ct_id, language, foil, condition):
    fresh = MarketPrice.objects.filter(
        blueprint_ct_id=blueprint_ct_id, language=language, foil=foil,
        fetched_at__gte=timezone.now() - PRICE_TTL,
    )
    exact = fresh.filter(condition=condition).first()
    if exact is not None:
        return exact.price
    return fresh.aggregate(p=Min("price"))["p"]


def price_for(client, blueprint_ct_id, language, foil, condition):
    """Cheapest comparable offer (same condition preferred), from the cache or one API call. None if unknown."""
    price = _lookup(blueprint_ct_id, language, foil, condition)
    if price is not None:
        return price
    try:
        offers = client.marketplace_products(blueprint_ct_id, language=LANGUAGE_TO_CT[language], foil=foil)
    except CardTraderError as exc:
        SyncLog.objects.create(level=SyncLog.Level.WARNING, message=f"Market price unavailable: {exc}"[:300])
        return None
    _store(blueprint_ct_id, _cheapest(offers, language=language, foil=foil))
    return _lookup(blueprint_ct_id, language, foil, condition)


PAUSE = 0.06  # CardTrader allows 200 requests per 10 s across all endpoints
RATE_LIMIT_WAIT = 10


def owned_combinations():
    """Distinct (blueprint ct_id, language, foil, is_product) of every user's collection items tied to the catalog.

    Prices are shared, so every owner counts. Products are never foil on CardTrader.
    """
    from .models import CollectionItem

    rows = (CollectionItem.objects.filter(card__ct_blueprint_id__isnull=False)
            .values_list("card__ct_blueprint_id", "language", "foil", "card__is_product").distinct())
    return sorted({(bp, lang, foil and not product, product) for bp, lang, foil, product in rows})


def _refresh_retrying(client, combination):
    bp, language, foil, is_product = combination
    try:
        return refresh(client, bp, language, foil, is_product=is_product)
    except CardTraderError as exc:
        if exc.status != 429:
            raise
        time.sleep(RATE_LIMIT_WAIT)
        return refresh(client, bp, language, foil, is_product=is_product)


def refresh_owned(client):
    """Refresh the prices of every owned combination, one CardTrader call each. Returns (refreshed, failed)."""
    refreshed = failed = 0
    for combination in owned_combinations():
        try:
            _refresh_retrying(client, combination)
        except CardTraderError:
            failed += 1
        else:
            refreshed += 1
        time.sleep(PAUSE)
    return refreshed, failed
