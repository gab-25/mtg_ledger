"""Nightly card database and Cardmarket prices.

CardTrader is the master card database: every MTG expansion and every blueprint (printing) is reloaded from its
API. Each blueprint carries its Cardmarket product ids, which tie it to Cardmarket's public daily price guide.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import httpx

from . import services
from .cardtrader import MTG_GAME_ID, CardTraderError
from .catalog import _blueprint_fields, category_pks, normalize_number
from .models import Blueprint, Card, CardmarketPrice, Category, Expansion, SyncLog

PRICE_GUIDE_URL = "https://downloads.s3.cardmarket.com/productCatalog/priceGuide/price_guide_1.json"
USER_AGENT = "mtg-ledger/0.1"

# Price guide key -> CardmarketPrice field
PRICE_FIELDS = {
    "low": "low", "trend": "trend", "avg": "avg", "avg1": "avg1", "avg7": "avg7", "avg30": "avg30",
    "low-foil": "low_foil", "trend-foil": "trend_foil", "avg-foil": "avg_foil",
    "avg1-foil": "avg1_foil", "avg7-foil": "avg7_foil", "avg30-foil": "avg30_foil",
}
BATCH = 2000
BLUEPRINT_PAUSE = 0.06  # CardTrader allows 200 requests per 10 s across all endpoints
RATE_LIMIT_WAIT = 10

logger = logging.getLogger(__name__)


class CardmarketError(Exception):
    pass


@dataclass
class SyncResult:
    expansions: int
    blueprints: int
    priced: int
    linked: int
    unlinked: int
    guide_date: datetime
    failed: list = field(default_factory=list)


def make_client(transport=None):
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=120, follow_redirects=True, transport=transport,
    )


# --- lookups ---------------------------------------------------------------

def blueprint_for(card):
    """The CardTrader printing of a Ledger card: its matched blueprint, else set code + collector number."""
    if card.ct_blueprint_id:
        bp = Blueprint.objects.filter(ct_id=card.ct_blueprint_id).first()
        if bp is not None:
            return bp
    number = normalize_number(card.collector_number)
    if not number:
        return None
    return Blueprint.objects.filter(expansion__code=card.set_code.lower(), collector_number=number).first()


def suggested_price(card, foil):
    """Cardmarket trend to suggest when listing a copy of card (foil trend for foil copies), or None."""
    bp = blueprint_for(card)
    if bp is None or bp.cm_price_id is None:
        return None
    price = CardmarketPrice.objects.filter(pk=bp.cm_price_id).first()
    if price is None:
        return None
    return price.trend_foil if foil else price.trend


def owned_quantities(owner):
    """{blueprint ct_id: copies in owner's collection}, matching cards like blueprint_for()."""
    from django.db.models import Q, Sum

    per_card = list(
        Card.objects.annotate(qty=Sum("items__quantity", filter=Q(items__owner=owner))).filter(qty__gt=0)
        .values_list("ct_blueprint_id", "set_code", "collector_number", "qty")
    )
    known = set(Blueprint.objects.filter(ct_id__in=[c[0] for c in per_card if c[0]]).values_list("ct_id", flat=True))
    wanted = {(s.lower(), normalize_number(n)) for bp, s, n, _ in per_card if bp not in known and normalize_number(n)}
    by_printing = {}
    if wanted:
        rows = Blueprint.objects.filter(
            expansion__code__in={s for s, _ in wanted}, collector_number__in={n for _, n in wanted}
        ).order_by("-ct_id").values_list("expansion__code", "collector_number", "ct_id")
        by_printing = {(s, n): ct_id for s, n, ct_id in rows}
    owned = {}
    for bp, s, n, qty in per_card:
        ct_id = bp if bp in known else by_printing.get((s.lower(), normalize_number(n)))
        if ct_id is not None:
            owned[ct_id] = owned.get(ct_id, 0) + qty
    return owned


# --- download ----------------------------------------------------------------

def _money(value):
    return None if value is None else Decimal(str(value))


def _fetch_guide(client):
    """({idProduct: CardmarketPrice}, createdAt) from today's price guide."""
    try:
        response = client.get(PRICE_GUIDE_URL)
        response.raise_for_status()
        guide = response.json()
        guide_date = datetime.strptime(guide["createdAt"], "%Y-%m-%dT%H:%M:%S%z")
        prices = {
            row["idProduct"]: CardmarketPrice(
                id_product=row["idProduct"], prices_date=guide_date,
                **{f: _money(row.get(key)) for key, f in PRICE_FIELDS.items()},
            )
            for row in guide["priceGuides"]
        }
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise CardmarketError(f"price guide: {type(exc).__name__}: {exc}") from exc
    return prices, guide_date


def _export(client, expansion_id):
    try:
        return client.blueprints(expansion_id) or []
    except CardTraderError as exc:
        if exc.status != 429:
            raise
        time.sleep(RATE_LIMIT_WAIT)
        return client.blueprints(expansion_id) or []


def _fetch_catalog(client):
    """(MTG expansions, {expansion ct_id: [blueprint]}, codes of expansions that could not be loaded)."""
    try:
        expansions = [e for e in client.expansions() if e.get("game_id") == MTG_GAME_ID and e.get("code")]
    except CardTraderError as exc:
        raise CardmarketError(f"CardTrader expansions: {exc}") from exc
    blueprints, failed = {}, []
    for e in expansions:
        try:
            blueprints[e["id"]] = _export(client, e["id"])
        except CardTraderError as exc:
            logger.warning("Blueprints of %s not loaded: %s", e["code"], exc)
            failed.append(e["code"].lower())
        time.sleep(BLUEPRINT_PAUSE)
    return expansions, blueprints, sorted(failed)


# --- write -------------------------------------------------------------------

def _count_links():
    """(linked, unlinked) Ledger cards against the local catalog."""
    ids = set(Blueprint.objects.values_list("ct_id", flat=True))
    printings = set(Blueprint.objects.values_list("expansion__code", "collector_number"))
    linked = unlinked = 0
    for card in Card.objects.only("set_code", "collector_number", "ct_blueprint_id"):
        number = normalize_number(card.collector_number)
        if card.ct_blueprint_id in ids or (number and (card.set_code.lower(), number) in printings):
            linked += 1
        else:
            unlinked += 1
    return linked, unlinked


def sync(cm_client, ct_client):
    """Reload the card database from CardTrader and the prices from Cardmarket.

    Everything is downloaded before anything is written; an expansion whose blueprints fail keeps what it had.
    """
    from django.db import transaction
    from django.utils import timezone

    prices, guide_date = _fetch_guide(cm_client)
    expansions, exports, failed = _fetch_catalog(ct_client)
    try:
        categories = ct_client.categories() or []
    except CardTraderError as exc:
        raise CardmarketError(f"CardTrader categories: {exc}") from exc
    now = timezone.now()
    try:
        expansion_objs = [Expansion(ct_id=e["id"], code=e["code"].lower(), name=e["name"]) for e in expansions]
        with transaction.atomic():
            Expansion.objects.bulk_create(
                expansion_objs, update_conflicts=True, unique_fields=["ct_id"], update_fields=["code", "name"],
            )
            Category.objects.bulk_create(
                [Category(ct_id=c["id"], name=c.get("name") or "") for c in categories],
                update_conflicts=True, unique_fields=["ct_id"], update_fields=["name"],
            )
            pk_by_ct = dict(Expansion.objects.values_list("ct_id", "pk"))
            category_by_ct = category_pks(bp.get("category_id") for bps in exports.values() for bp in bps)
            objs = {}
            for exp_ct_id, bps in exports.items():
                for bp in bps:
                    objs[bp["id"]] = Blueprint(
                        ct_id=bp["id"], expansion_id=pk_by_ct[exp_ct_id], **_blueprint_fields(bp, category_by_ct),
                    )
            Blueprint.objects.bulk_create(
                objs.values(), batch_size=BATCH, update_conflicts=True, unique_fields=["ct_id"],
                update_fields=[
                    "expansion", "name", "collector_number", "version", "rarity", "image_url", "cm_price",
                    "category", "editable_properties",
                ],
            )
            Expansion.objects.filter(ct_id__in=exports.keys()).update(blueprints_synced_at=now)
            CardmarketPrice.objects.bulk_create(
                prices.values(), batch_size=BATCH, update_conflicts=True, unique_fields=["id_product"],
                update_fields=[*PRICE_FIELDS.values(), "prices_date"],
            )
    except (KeyError, TypeError) as exc:  # unexpected blueprint shape, raised before the transaction commits
        raise CardmarketError(f"CardTrader blueprints: {type(exc).__name__}: {exc}") from exc
    linked, unlinked = _count_links()
    priced = sum(1 for o in objs.values() if o.cm_price_id in prices)
    return SyncResult(
        expansions=len(expansions), blueprints=len(objs), priced=priced, linked=linked, unlinked=unlinked,
        guide_date=guide_date, failed=failed,
    )


def _summary(result):
    message = (
        f"Cardmarket: {result.expansions} expansions, {result.blueprints} printings, {result.priced} priced "
        f"(guide {result.guide_date:%Y-%m-%d}), {result.linked} cards linked, {result.unlinked} not linked"
    )
    if result.failed:
        shown = ", ".join(result.failed[:10]) + (", …" if len(result.failed) > 10 else "")
        message += f"; {len(result.failed)} expansions not loaded ({shown})"
    return message


def run_sync(cm_client=None):
    """sync() with its outcome written to the SyncLog. Returns the result, or None when it could not run."""
    own_cm = cm_client is None
    cm_client = cm_client or make_client()
    ct_client = None
    try:
        ct_client = services.service_client()
        result = sync(cm_client, ct_client)
    except (CardmarketError, CardTraderError) as exc:
        SyncLog.objects.create(message=f"Cardmarket sync failed: {exc}"[:300], level=SyncLog.Level.WARNING)
        return None
    except Exception as exc:
        # Unexpected (database, bug): still visible in the Activity log, and the task is marked failed.
        logger.exception("Cardmarket sync failed")
        SyncLog.objects.create(
            message=f"Cardmarket sync failed: {type(exc).__name__}: {exc}"[:300], level=SyncLog.Level.WARNING
        )
        raise
    finally:
        if ct_client is not None:
            ct_client.close()
        if own_cm:
            cm_client.close()
    level = SyncLog.Level.WARNING if result.failed else SyncLog.Level.OK
    SyncLog.objects.create(message=_summary(result)[:300], level=level)
    return result
