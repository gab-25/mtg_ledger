"""Nightly catalog sync: the card database from CardTrader and the prices from Cardmarket.

CardTrader is the master card database: every MTG expansion and every blueprint (printing) is reloaded from its
API, then tied to Cardmarket's public daily price guide (ledger/cardmarket.py). The outcome goes to the SyncLog.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from . import cardmarket, services
from .cardmarket import PRICE_FIELDS, CardmarketError
from .cardtrader import MTG_GAME_ID, CardTraderError
from .catalog import _blueprint_fields, category_pks, normalize_number, refresh_product_flags
from .models import Blueprint, Card, CardmarketPrice, Category, Expansion, SyncLog

BATCH = 2000
BLUEPRINT_PAUSE = 0.06  # CardTrader allows 200 requests per 10 s across all endpoints
RATE_LIMIT_WAIT = 10

logger = logging.getLogger(__name__)


class CatalogSyncError(Exception):
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


# --- download ----------------------------------------------------------------

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
        raise CatalogSyncError(f"CardTrader expansions: {exc}") from exc
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

    prices, guide_date = cardmarket.fetch_guide(cm_client)
    expansions, exports, failed = _fetch_catalog(ct_client)
    try:
        categories = ct_client.categories() or []
    except CardTraderError as exc:
        raise CatalogSyncError(f"CardTrader categories: {exc}") from exc
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
            refresh_product_flags(objs.keys())
            Expansion.objects.filter(ct_id__in=exports.keys()).update(blueprints_synced_at=now)
            CardmarketPrice.objects.bulk_create(
                prices.values(), batch_size=BATCH, update_conflicts=True, unique_fields=["id_product"],
                update_fields=[*PRICE_FIELDS.values(), "prices_date"],
            )
    except (KeyError, TypeError) as exc:  # unexpected blueprint shape, raised before the transaction commits
        raise CatalogSyncError(f"CardTrader blueprints: {type(exc).__name__}: {exc}") from exc
    linked, unlinked = _count_links()
    priced = sum(1 for o in objs.values() if o.cm_price_id in prices)
    return SyncResult(
        expansions=len(expansions), blueprints=len(objs), priced=priced, linked=linked, unlinked=unlinked,
        guide_date=guide_date, failed=failed,
    )


def _summary(result):
    message = (
        f"Catalog sync: {result.expansions} expansions, {result.blueprints} printings, {result.priced} priced "
        f"(guide {result.guide_date:%Y-%m-%d}), {result.linked} cards linked, {result.unlinked} not linked"
    )
    if result.failed:
        shown = ", ".join(result.failed[:10]) + (", …" if len(result.failed) > 10 else "")
        message += f"; {len(result.failed)} expansions not loaded ({shown})"
    return message


def run_sync(cm_client=None):
    """sync() with its outcome written to the SyncLog. Returns the result, or None when it could not run."""
    own_cm = cm_client is None
    cm_client = cm_client or cardmarket.make_client()
    ct_client = None
    try:
        ct_client = services.service_client()
        result = sync(cm_client, ct_client)
    except (CatalogSyncError, CardmarketError, CardTraderError) as exc:
        SyncLog.objects.create(message=f"Catalog sync failed: {exc}"[:300], level=SyncLog.Level.WARNING)
        return None
    except Exception as exc:
        # Unexpected (database, bug): still visible in the Activity log, and the task is marked failed.
        logger.exception("Catalog sync failed")
        SyncLog.objects.create(
            message=f"Catalog sync failed: {type(exc).__name__}: {exc}"[:300], level=SyncLog.Level.WARNING
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
