"""Cardmarket catalog and price guide (public daily files) plus the Scryfall bridge.

Cardmarket publishes its MTG singles and their price guide every night, but the product list has no set code or
collector number. Scryfall's bulk data gives both for each `cardmarket_id`, which is how products are tied to the
Ledger's cards (set + collector number).
"""

import gzip
import json
import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import httpx

from .catalog import normalize_number
from .models import Card, CardmarketProduct, SyncLog

PRODUCTS_URL = "https://downloads.s3.cardmarket.com/productCatalog/productList/products_singles_1.json"
PRICE_GUIDE_URL = "https://downloads.s3.cardmarket.com/productCatalog/priceGuide/price_guide_1.json"
SCRYFALL_BULK_URL = "https://api.scryfall.com/bulk-data"
USER_AGENT = "mtg-ledger/0.1"

# Price guide key -> CardmarketProduct field
PRICE_FIELDS = {
    "low": "low", "trend": "trend", "avg": "avg", "avg1": "avg1", "avg7": "avg7", "avg30": "avg30",
    "low-foil": "low_foil", "trend-foil": "trend_foil", "avg-foil": "avg_foil",
    "avg1-foil": "avg1_foil", "avg7-foil": "avg7_foil", "avg30-foil": "avg30_foil",
}
BATCH = 2000

logger = logging.getLogger(__name__)


class CardmarketError(Exception):
    pass


@dataclass
class SyncResult:
    products: int
    priced: int
    linked: int
    unlinked: int
    guide_date: datetime


def make_client(transport=None):
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=120, follow_redirects=True, transport=transport,
    )


def suggested_price(card, foil):
    """Cardmarket trend to suggest when listing a copy of card (foil trend for foil copies), or None."""
    product = card.cm_product
    if product is None:
        return None
    return product.trend_foil if foil else product.trend


def _get_json(client, url):
    response = client.get(url)
    response.raise_for_status()
    return response.json()


def _image(card):
    uris = card.get("image_uris") or ((card.get("card_faces") or [{}])[0].get("image_uris")) or {}
    return uris.get("normal", "")


def _scryfall_printings(client):
    """{cardmarket_id: (set code, collector number, image url)} from Scryfall default_cards (gzipped JSON lines),
    downloaded to a temporary file and read one card at a time."""
    bulk = _get_json(client, SCRYFALL_BULK_URL)
    uri = next(d["jsonl_download_uri"] for d in bulk["data"] if d["type"] == "default_cards")
    printings = {}
    with tempfile.TemporaryFile() as f:
        with client.stream("GET", uri) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                f.write(chunk)
        f.seek(0)
        compressed = f.read(2) == b"\x1f\x8b"  # already inflated when served with Content-Encoding: gzip
        f.seek(0)
        lines = gzip.open(f, "rt", encoding="utf-8") if compressed else (line.decode("utf-8") for line in f)
        for line in lines:
            if not line.strip():
                continue
            card = json.loads(line)
            cm_id = card.get("cardmarket_id")
            if cm_id is None or cm_id in printings:
                continue
            printings[cm_id] = (
                (card.get("set") or "").lower(), normalize_number(card.get("collector_number")), _image(card)
            )
    return printings


def _money(value):
    return None if value is None else Decimal(str(value))


def _fetch(client):
    """Download and parse every source; raises CardmarketError before anything is written."""
    try:
        # One row per product: a repeated idProduct would make the upsert touch the same row twice.
        products = list({p["idProduct"]: p for p in reversed(_get_json(client, PRODUCTS_URL)["products"])}.values())
        guide = _get_json(client, PRICE_GUIDE_URL)
        guide_date = datetime.strptime(guide["createdAt"], "%Y-%m-%dT%H:%M:%S%z")
        prices = {row["idProduct"]: row for row in guide["priceGuides"]}
        printings = _scryfall_printings(client)
        objs = []
        for p in products:
            set_code, number, image = printings.get(p["idProduct"], ("", "", ""))
            row = prices.get(p["idProduct"], {})
            objs.append(CardmarketProduct(
                id_product=p["idProduct"], name=p["name"][:200], id_expansion=p.get("idExpansion"),
                set_code=set_code[:10], collector_number=number[:12], image_url=image[:500],
                prices_date=guide_date,
                **{field: _money(row.get(key)) for key, field in PRICE_FIELDS.items()},
            ))
    except (httpx.HTTPError, OSError, EOFError, ValueError, KeyError, TypeError, StopIteration) as exc:
        raise CardmarketError(f"{type(exc).__name__}: {exc}") from exc
    priced = sum(1 for p in products if p["idProduct"] in prices)
    return objs, priced, guide_date


def _relink_cards():
    """Point every Card at the product with its set + collector number. Returns (linked, unlinked)."""
    by_printing = dict(
        ((s, n), pk) for pk, s, n in
        CardmarketProduct.objects.exclude(set_code="").values_list("pk", "set_code", "collector_number")
    )
    changed, linked, unlinked = [], 0, 0
    for card in Card.objects.only("pk", "set_code", "collector_number", "cm_product"):
        number = normalize_number(card.collector_number)
        target = by_printing.get((card.set_code.lower(), number)) if number else None
        if target is None:
            unlinked += 1
        else:
            linked += 1
        if card.cm_product_id != target:
            card.cm_product_id = target
            changed.append(card)
    Card.objects.bulk_update(changed, ["cm_product"], batch_size=BATCH)
    return linked, unlinked


def sync(client):
    """Refresh products and prices from today's files and relink the Ledger's cards (all or nothing)."""
    from django.db import transaction

    objs, priced, guide_date = _fetch(client)
    update_fields = [f.name for f in CardmarketProduct._meta.concrete_fields if not f.primary_key]
    with transaction.atomic():
        CardmarketProduct.objects.bulk_create(
            objs, batch_size=BATCH, update_conflicts=True, unique_fields=["id_product"],
            update_fields=update_fields,
        )
        linked, unlinked = _relink_cards()
    return SyncResult(products=len(objs), priced=priced, linked=linked, unlinked=unlinked, guide_date=guide_date)


def run_sync(client=None):
    """sync() with its outcome written to the SyncLog. Returns the result, or None when a source failed."""
    own = client is None
    client = client or make_client()
    try:
        result = sync(client)
    except CardmarketError as exc:
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
        if own:
            client.close()
    SyncLog.objects.create(message=(
        f"Cardmarket: {result.products} products, {result.priced} priced (guide {result.guide_date:%Y-%m-%d}), "
        f"{result.linked} cards linked, {result.unlinked} not linked"
    ))
    return result

