"""Market price cache: cheapest CardTrader offers per printing, language, foil and condition."""

from datetime import timedelta

from django.db.models import Min
from django.utils import timezone

from .cardtrader import CONDITION_FROM_CT, LANGUAGE_TO_CT, CardTraderError, money
from .models import MarketPrice, SyncLog

PRICE_TTL = timedelta(hours=6)
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
