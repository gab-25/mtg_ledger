"""Business operations: CSV import/export and CardTrader sync."""

import csv
import io
import time
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .cardtrader import (
    CONDITION_FROM_CT,
    CONDITION_TO_CT,
    LANGUAGE_TO_CT,
    MTG_GAME_ID,
    CardTraderClient,
    CardTraderError,
    money,
)
from .openrouter import OpenRouterClient
from .catalog import blueprint_number, merge_cards, normalize_number
from .forms import get_or_create_card
from .models import (
    Card,
    CollectionItem,
    Color,
    Condition,
    Language,
    Listing,
    Order,
    OrderItem,
    SyncConflict,
    SyncLog,
    SyncService,
)

OPENROUTER = "OpenRouter"
CSV_FIELDS = ["name", "set", "type", "color", "condition", "language", "foil", "quantity", "price", "number"]
CARDTRADER = "CardTrader"


def get_cardtrader():
    service, _ = SyncService.objects.get_or_create(
        name=CARDTRADER,
        defaults={"description": "Sale listings and orders", "direction": "Ledger ⇄ CardTrader (API)"},
    )
    return service


def api_token(service=None):
    service = service or get_cardtrader()
    return service.api_token or getattr(settings, "CARDTRADER_API_TOKEN", "")


def make_client(service=None, token=None):
    """Build the API client. Tests patch this to inject a mock transport."""
    return CardTraderClient(token or api_token(service))


def set_api_token(token):
    """Check the token against CardTrader (/info), then store it. Returns the app info."""
    token = token.strip()
    with make_client(token=token) as client:
        info = client.info() or {}
    service = get_cardtrader()
    service.api_token = token
    service.save(update_fields=["api_token"])
    _log(f"{CARDTRADER}: API token connected" + (f" (app {info['name']})" if info.get("name") else ""))
    return info


def remove_api_token():
    service = get_cardtrader()
    service.api_token = ""
    service.save(update_fields=["api_token"])
    _log(f"{CARDTRADER}: API token removed", SyncLog.Level.WARNING)


def get_openrouter():
    service, _ = SyncService.objects.get_or_create(
        name=OPENROUTER, defaults={"description": "Card recognition", "direction": "Camera → OpenRouter (API)"},
    )
    return service


def openrouter_key(service=None):
    service = service or get_openrouter()
    return service.api_token or getattr(settings, "OPENROUTER_API_KEY", "")


def make_openrouter(key=None):
    """Build the OpenRouter client. Tests patch this to inject a fake."""
    return OpenRouterClient(key or openrouter_key(), model=settings.OPENROUTER_MODEL)


def check_openrouter_key(key):
    with make_openrouter(key) as client:
        client.check_key()


def set_openrouter_key(key):
    """Check the key with OpenRouter, then store it. Raises OpenRouterError if rejected."""
    key = key.strip()
    check_openrouter_key(key)
    service = get_openrouter()
    service.api_token = key
    service.save(update_fields=["api_token"])
    _log(f"{OPENROUTER}: API key connected")


def remove_openrouter_key():
    service = get_openrouter()
    service.api_token = ""
    service.save(update_fields=["api_token"])
    _log(f"{OPENROUTER}: API key removed", SyncLog.Level.WARNING)


def _log(message, level=SyncLog.Level.OK):
    SyncLog.objects.create(message=message[:300], level=level)


# --- CSV -----------------------------------------------------------------

def export_csv(items, stream):
    writer = csv.writer(stream)
    writer.writerow(CSV_FIELDS)
    for item in items.select_related("card"):
        writer.writerow([
            item.card.name,
            item.card.set_code,
            item.card.type_line,
            item.card.color,
            item.condition,
            item.language,
            "1" if item.foil else "0",
            item.quantity,
            f"{item.price:.2f}",
            item.card.collector_number,
        ])


class CsvImportError(ValueError):
    pass


def _choice(value, choices, default, field_name, line):
    value = (value or "").strip().upper()
    if not value:
        return default
    if value not in choices.values:
        raise CsvImportError(f"Line {line}: invalid {field_name} '{value}'")
    return value


def import_csv(uploaded_file):
    """Create or update collection items from a CSV. Returns (created, updated)."""
    try:
        text = uploaded_file.read().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvImportError("The file is not UTF-8 encoded") from exc

    reader = csv.DictReader(io.StringIO(text))
    header = {h.strip().lower() for h in (reader.fieldnames or [])}
    missing = {"name", "set"} - header
    if missing:
        raise CsvImportError(f"Missing required columns: {', '.join(sorted(missing))}")

    created = updated = 0
    with transaction.atomic():
        for line, raw in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            if not row.get("name"):
                continue
            try:
                quantity = int(row.get("quantity") or 1)
                price = Decimal(row.get("price") or "0").quantize(Decimal("0.01"))
            except (ValueError, InvalidOperation) as exc:
                raise CsvImportError(f"Line {line}: invalid quantity or price") from exc
            if quantity < 0 or price < 0:
                raise CsvImportError(f"Line {line}: quantity and price must be positive")

            card = get_or_create_card(
                row["name"],
                row["set"].upper(),
                normalize_number(row.get("number", "")),
                type_line=row.get("type", ""),
                color=_choice(row.get("color"), Color, Color.COLORLESS, "color", line),
            )
            item, item_created = CollectionItem.objects.get_or_create(
                card=card,
                condition=_choice(row.get("condition"), Condition, Condition.NEAR_MINT, "condition", line),
                language=_choice(row.get("language"), Language, Language.EN, "language", line),
                foil=row.get("foil", "").lower() in {"1", "true", "yes", "y", "foil"},
                defaults={"quantity": quantity, "price": price},
            )
            if item_created:
                created += 1
            else:
                item.quantity = quantity
                item.price = price
                item.save(update_fields=["quantity", "price", "updated_at"])
                updated += 1

        _log(f"CSV import: {created} new cards, {updated} quantities updated")
    return created, updated


# --- Catalog matching ------------------------------------------------------

def resolve_blueprints(client, cards):
    """Fill Card.ct_blueprint_id from CardTrader's catalog. Returns cards that could not be matched."""
    todo = [c for c in cards if not c.ct_blueprint_id]
    if not todo:
        return []
    expansions = {
        e["code"].lower(): e["id"]
        for e in client.expansions()
        if e.get("game_id") == MTG_GAME_ID and e.get("code")
    }
    unmatched = []
    by_set = {}
    for card in todo:
        by_set.setdefault(card.set_code.lower(), []).append(card)
    for set_code, set_cards in by_set.items():
        expansion_id = expansions.get(set_code)
        if expansion_id is None:
            unmatched.extend(set_cards)
            continue
        by_name, by_number = {}, {}
        for bp in client.blueprints(expansion_id):
            # Several blueprints can share a name (alternate versions): keep the base one.
            key = bp["name"].casefold()
            if key not in by_name or (by_name[key].get("version") and not bp.get("version")):
                by_name[key] = bp
            number = blueprint_number(bp)
            if number:
                by_number[number] = bp
        for card in set_cards:
            if card.collector_number:
                bp = by_number.get(card.collector_number)
            else:
                bp = by_name.get(card.name.casefold())
            if bp is None:
                unmatched.append(card)
                continue
            number = blueprint_number(bp) or card.collector_number
            existing = (
                Card.objects.exclude(pk=card.pk)
                .filter(Q(ct_blueprint_id=bp["id"]) | Q(set_code=card.set_code, collector_number=number) & ~Q(collector_number=""))
                .first()
            )
            if existing is not None and existing.ct_blueprint_id not in (None, bp["id"]):
                _log(f"{card} matches #{number}, already used by {existing} (another printing): fix it by hand",
                     SyncLog.Level.WARNING)
                unmatched.append(card)
                continue
            if existing is not None:
                if not merge_cards(card, existing):
                    _log(f"{card} duplicates {existing} but both have live listings: merge them by hand",
                         SyncLog.Level.WARNING)
                    unmatched.append(card)
                    continue
                if not existing.ct_blueprint_id:
                    existing.ct_blueprint_id = bp["id"]
                    existing.save(update_fields=["ct_blueprint_id"])
                continue
            card.ct_blueprint_id = bp["id"]
            card.collector_number = number
            card.save(update_fields=["ct_blueprint_id", "collector_number"])
    return unmatched


# --- Listings ------------------------------------------------------------

def product_payload(listing):
    item = listing.item
    properties = {
        "condition": CONDITION_TO_CT[item.condition],
        "mtg_language": LANGUAGE_TO_CT[item.language],
    }
    if item.foil:
        properties["mtg_foil"] = True
    payload = {
        "price": float(listing.price),
        "quantity": listing.quantity,
        "properties": properties,
        "error_mode": "strict",
        "user_data_field": f"mtg-ledger:{listing.pk}",
    }
    if listing.ct_product_id:
        payload["id"] = listing.ct_product_id
    else:
        payload["blueprint_id"] = item.card.ct_blueprint_id
    return payload


def _job_error(result):
    errors = result.get("errors") or {}
    if isinstance(errors, dict):
        return "; ".join(f"{k}: {', '.join(map(str, v)) if isinstance(v, list) else v}" for k, v in errors.items())
    return str(errors) or "unknown error"


@dataclass
class PublishResult:
    published: int = 0
    failed: list = field(default_factory=list)  # [(listing, reason)]


def publish_pending():
    """Push every listing waiting to be published to CardTrader."""
    service = get_cardtrader()
    listings = list(
        Listing.objects.filter(status=Listing.Status.TO_PUBLISH).select_related("item__card")
    )
    result = PublishResult()
    if not listings:
        return result

    with make_client(service) as client:
        unmatched = {c.pk for c in resolve_blueprints(client, {l.item.card for l in listings})}
        # Resolution may merge duplicate printings: reload so listings point at the surviving cards.
        listings = list(
            Listing.objects.filter(pk__in=[l.pk for l in listings]).select_related("item__card")
        )
        to_create, to_update = [], []
        for listing in listings:
            if listing.ct_product_id:
                to_update.append(listing)
            elif listing.item.card_id in unmatched or not listing.item.card.ct_blueprint_id:
                result.failed.append((listing, "card not found in the CardTrader catalog"))
            else:
                to_create.append(listing)

        for batch, call in ((to_create, client.bulk_create), (to_update, client.bulk_update)):
            if not batch:
                continue
            outcomes = call([product_payload(l) for l in batch])
            for listing, outcome in zip(batch, outcomes):
                if outcome.get("result") in ("ok", "warning") and outcome.get("product_id"):
                    listing.ct_product_id = outcome["product_id"]
                    listing.status = Listing.Status.PUBLISHED
                    listing.save(update_fields=["ct_product_id", "status", "updated_at"])
                    result.published += 1
                else:
                    result.failed.append((listing, _job_error(outcome)))

    service.last_sync = timezone.now()
    service.save(update_fields=["last_sync"])
    _log(f"{CARDTRADER}: {result.published} listings published")
    for listing, reason in result.failed:
        _log(f"{CARDTRADER}: {listing.item.card.name} not published ({reason})", SyncLog.Level.WARNING)
    return result


def remove_listing(listing):
    """Delete a listing locally and, if it was published, on CardTrader."""
    if listing.ct_product_id and listing.status != Listing.Status.SOLD:
        with make_client() as client:
            try:
                client.delete_product(listing.ct_product_id)
            except CardTraderError as exc:
                if exc.status != 404:  # already gone remotely: fine
                    raise
        _log(f"{CARDTRADER}: listing for {listing.item.card.name} removed")
    listing.delete()


def _market_price(products, condition, my_user_id):
    others = [p for p in products if (p.get("user") or {}).get("id") != my_user_id]
    same = [p for p in others if (p.get("properties_hash") or {}).get("condition") == condition]
    pool = same or others
    prices = [money(p.get("price")) for p in pool if p.get("price")]
    return min(prices) if prices else None


MARKETPLACE_PAUSE = 0.11  # marketplace endpoint: max 10 requests per second


def refresh_market_prices(client, listings):
    """Update market_price from the cheapest comparable offer on CardTrader."""
    my_user_id = (client.info() or {}).get("user_id")
    updated = 0
    for listing in listings:
        item = listing.item
        if not item.card.ct_blueprint_id:
            continue
        products = client.marketplace_products(
            item.card.ct_blueprint_id, language=LANGUAGE_TO_CT[item.language], foil=item.foil
        )
        price = _market_price(products, CONDITION_TO_CT[item.condition], my_user_id)
        if price is not None and price != listing.market_price:
            listing.market_price = price
            listing.save(update_fields=["market_price", "updated_at"])
            updated += 1
        time.sleep(MARKETPLACE_PAUSE)
    return updated


def match_market_prices():
    """Refresh market prices from CardTrader, then align active listings to them."""
    active = Listing.objects.exclude(status=Listing.Status.SOLD).select_related("item__card")
    with make_client() as client:
        resolve_blueprints(client, {l.item.card for l in active})
        refresh_market_prices(client, list(active))
    with transaction.atomic():
        changed = (
            Listing.objects.exclude(status=Listing.Status.SOLD)
            .exclude(market_price__isnull=True)
            .exclude(price=F("market_price"))
        )
        count = changed.update(
            price=F("market_price"), status=Listing.Status.TO_PUBLISH, updated_at=timezone.now()
        )
        if count:
            _log(f"{count} listing prices matched to market")
    return count


# --- Orders / shipments ----------------------------------------------------

def _dt(value):
    return parse_datetime(value) if value else None


def _decrements_stock(order):
    """CardTrader's rule: CT Zero orders count while hub_pending (later merged/closed);
    the weekly merged CT Zero order and regular orders follow the normal paid flow."""
    if order.via_cardtrader_zero:
        return order.state in (Order.State.HUB_PENDING, Order.State.CLOSED)
    return order.state in (Order.State.PAID, Order.State.SENT, Order.State.ARRIVED, Order.State.DONE)


def _apply_stock(order):
    for oi in order.items.select_related("listing__item"):
        listing = oi.listing
        if listing is None:
            continue
        item = listing.item
        listing.quantity = max(listing.quantity - oi.quantity, 0)
        item.quantity = max(item.quantity - oi.quantity, 0)
        if listing.quantity == 0:
            listing.status = Listing.Status.SOLD
            listing.sold_at = order.paid_at or timezone.now()
        listing.save(update_fields=["quantity", "status", "sold_at", "updated_at"])
        item.save(update_fields=["quantity", "updated_at"])
    order.stock_applied = True
    order.save(update_fields=["stock_applied"])


def upsert_order(data):
    """Create/update an Order from the CardTrader payload. Returns (order, created)."""
    address = data.get("order_shipping_address") or {}
    method = data.get("order_shipping_method") or {}
    buyer = data.get("buyer") or {}
    total = data.get("seller_total") or {}
    order, created = Order.objects.update_or_create(
        ct_id=data["id"],
        defaults={
            "code": data.get("code") or "",
            "state": data.get("state") or "",
            "via_cardtrader_zero": bool(data.get("via_cardtrader_zero")),
            "buyer_username": buyer.get("username") or "",
            "buyer_email": buyer.get("email") or "",
            "buyer_phone": buyer.get("phone") or "",
            "ship_name": address.get("name") or "",
            "ship_street": address.get("street") or "",
            "ship_zip": address.get("zip") or "",
            "ship_city": address.get("city") or "",
            "ship_province": address.get("state_or_province") or "",
            "ship_country": address.get("country") or "",
            "ship_country_code": address.get("country_code") or "",
            "ship_note": address.get("note") or "",
            "shipping_method": method.get("name") or "",
            "tracked": bool(method.get("tracked")),
            "tracking_code": method.get("tracking_code") or "",
            "max_shipping_days": method.get("max_estimate_shipping_days") or None,
            "packing_number": data.get("packing_number"),
            "size": data.get("size") or 0,
            "total": money(total),
            "currency": total.get("currency") or "",
            "paid_at": _dt(data.get("paid_at")),
            "sent_at": _dt(data.get("sent_at")),
            "created_at": _dt((data.get("order_items") or [{}])[0].get("created_at")),
            "raw": data,
        },
    )
    listings = {
        l.ct_product_id: l
        for l in Listing.objects.filter(
            ct_product_id__in=[i.get("product_id") for i in data.get("order_items") or []]
        )
    }
    for raw in data.get("order_items") or []:
        props = raw.get("properties") or {}
        OrderItem.objects.update_or_create(
            order=order,
            ct_id=raw["id"],
            defaults={
                "ct_product_id": raw.get("product_id"),
                "listing": listings.get(raw.get("product_id")),
                "name": raw.get("name") or "",
                "expansion": raw.get("expansion") or "",
                "quantity": raw.get("quantity") or 1,
                "price": money(raw.get("seller_price")),
                "condition": CONDITION_FROM_CT.get(props.get("condition"), props.get("condition") or ""),
                "language": (props.get("mtg_language") or "").upper(),
                "foil": bool(props.get("mtg_foil")),
            },
        )
    if not order.stock_applied and _decrements_stock(order):
        _apply_stock(order)
    return order, created


def import_orders(client, service):
    since = service.orders_synced_at - timedelta(days=14) if service.orders_synced_at else None
    created = updated = 0
    for data in client.all_orders(date_from=since.date() if since else None):
        with transaction.atomic():
            _, was_created = upsert_order(data)
        created += was_created
        updated += not was_created
    service.orders_synced_at = timezone.now()
    service.save(update_fields=["orders_synced_at"])
    to_ship = Order.objects.filter(state=Order.State.PAID).count()
    _log(f"{CARDTRADER}: {created} new orders, {updated} updated, {to_ship} to ship")
    return created, updated


def _diff_listings(service, products):
    """Open/refresh conflicts between Ledger listings and CardTrader products."""
    remote = {p["id"]: p for p in products}
    seen = set()
    listings = Listing.objects.filter(
        status=Listing.Status.PUBLISHED, ct_product_id__isnull=False
    ).select_related("item__card")
    for listing in listings:
        product = remote.get(listing.ct_product_id)
        diffs = []
        if product is None:
            diffs.append(("status", "Published", "Not on CardTrader", None))
        else:
            remote_price = money(product.get("price_cents"))
            if remote_price is not None and remote_price != listing.price:
                diffs.append(("price", f"€{listing.price}", f"€{remote_price}", remote_price))
            remote_qty = product.get("quantity")
            if remote_qty is not None and remote_qty != listing.quantity:
                diffs.append(("quantity", f"{listing.quantity} for sale", f"{remote_qty} for sale", Decimal(remote_qty)))
        for field_name, local, remote_value, amount in diffs:
            conflict, _ = SyncConflict.objects.update_or_create(
                service=service, item=listing.item, field=field_name, resolution=SyncConflict.Resolution.OPEN,
                defaults={"local_value": local, "remote_value": remote_value, "remote_amount": amount},
            )
            seen.add(conflict.pk)
    stale = SyncConflict.objects.filter(service=service, resolution=SyncConflict.Resolution.OPEN).exclude(pk__in=seen)
    stale.delete()
    return len(seen)


def sync_all():
    """Import orders (applying sold stock), then compare listings with CardTrader."""
    service = get_cardtrader()
    with make_client(service) as client:
        import_orders(client, service)
        conflicts = _diff_listings(service, client.my_products() or [])
    service.last_sync = timezone.now()
    service.save(update_fields=["last_sync"])
    if conflicts:
        _log(f"{CARDTRADER}: {conflicts} listings differ from Ledger", SyncLog.Level.WARNING)
    else:
        _log(f"{CARDTRADER}: listings in sync")
    return conflicts


def resolve_conflict(conflict, keep):
    """keep is 'ledger' or 'remote'."""
    listing = getattr(conflict.item, "listing", None)
    with transaction.atomic():
        if listing is not None:
            if keep == SyncConflict.Resolution.REMOTE:
                if conflict.field == "price" and conflict.remote_amount is not None:
                    listing.price = conflict.remote_amount
                elif conflict.field == "quantity" and conflict.remote_amount is not None:
                    listing.quantity = int(conflict.remote_amount)
                elif conflict.field == "status":
                    # Gone from CardTrader: treat as no longer listed.
                    listing.ct_product_id = None
                    listing.status = Listing.Status.SOLD
                    listing.sold_at = listing.sold_at or timezone.now()
                listing.save()
            else:
                # Ledger wins: push local values again on next publish.
                if conflict.field == "status":
                    listing.ct_product_id = None  # recreate the product
                listing.status = Listing.Status.TO_PUBLISH
                listing.save()
        conflict.resolution = keep
        conflict.save(update_fields=["resolution"])
        _log(
            f"{conflict.service.name}: conflict on {conflict.item.card.name} resolved "
            f"({conflict.get_resolution_display().lower()})"
        )


def ship_order(order, tracking_code=""):
    """Set the tracking code (if any) and mark the order as shipped on CardTrader."""
    tracking_code = tracking_code.strip()
    if (order.via_cardtrader_zero or order.tracked) and not (tracking_code or order.tracking_code):
        kind = "CardTrader Zero orders" if order.via_cardtrader_zero else "Tracked shipments"
        raise CardTraderError(f"{kind} need a tracking code before shipping.")
    with make_client() as client:
        if tracking_code and tracking_code != order.tracking_code:
            client.set_tracking_code(order.ct_id, tracking_code)
            order.tracking_code = tracking_code
            order.save(update_fields=["tracking_code", "updated_at"])
        data = client.ship(order.ct_id)
    if isinstance(data, dict) and data.get("id") == order.ct_id:
        order, _ = upsert_order(data)
    else:
        order.state = Order.State.SENT
        order.sent_at = timezone.now()
        order.save(update_fields=["state", "sent_at", "updated_at"])
    _log(f"{CARDTRADER}: order #{order.code} shipped")
    return order


def save_tracking_code(order, tracking_code):
    with make_client() as client:
        client.set_tracking_code(order.ct_id, tracking_code.strip())
    order.tracking_code = tracking_code.strip()
    order.save(update_fields=["tracking_code", "updated_at"])
    _log(f"{CARDTRADER}: tracking code set for order #{order.code}")
