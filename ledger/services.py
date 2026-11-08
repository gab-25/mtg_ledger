"""Business operations: CSV import/export and CardTrader sync.

The CardTrader side is not wired to the real API yet: publishing and syncing
update local state and the activity log, so the UI flow is complete and the
HTTP calls can be dropped into `CardTraderClient` later.
"""

import csv
import io
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import (
    Card,
    CollectionItem,
    Color,
    Condition,
    Language,
    Listing,
    SyncConflict,
    SyncLog,
    SyncService,
)

CSV_FIELDS = ["name", "set", "type", "color", "condition", "language", "foil", "quantity", "price"]
CARDTRADER = "CardTrader"


def get_cardtrader():
    service, _ = SyncService.objects.get_or_create(
        name=CARDTRADER,
        defaults={"description": "Sale listings", "direction": "Ledger → CardTrader (API)"},
    )
    return service


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
        ])


class CsvImportError(ValueError):
    pass


def _choice(value, choices, default, field, line):
    value = (value or "").strip().upper()
    if not value:
        return default
    if value not in choices.values:
        raise CsvImportError(f"Line {line}: invalid {field} '{value}'")
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

            card, card_created = Card.objects.get_or_create(
                name=row["name"],
                set_code=row["set"].upper(),
                defaults={
                    "type_line": row.get("type", ""),
                    "color": _choice(row.get("color"), Color, Color.COLORLESS, "color", line),
                },
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

        SyncLog.objects.create(
            message=f"CSV import: {created} new cards, {updated} quantities updated"
        )
    return created, updated


# --- Listings & sync -----------------------------------------------------

def publish_pending():
    """Push every listing waiting to be published. Returns the count."""
    with transaction.atomic():
        count = Listing.objects.filter(status=Listing.Status.TO_PUBLISH).update(
            status=Listing.Status.PUBLISHED, updated_at=timezone.now()
        )
        service = get_cardtrader()
        service.last_sync = timezone.now()
        service.save(update_fields=["last_sync"])
        SyncLog.objects.create(message=f"{CARDTRADER}: {count} listings published")
    return count


def match_market_prices():
    """Align active listing prices to the market price; changed ones need publishing."""
    with transaction.atomic():
        listings = (
            Listing.objects.exclude(status=Listing.Status.SOLD)
            .exclude(market_price__isnull=True)
            .exclude(price=F("market_price"))
        )
        count = listings.update(
            price=F("market_price"), status=Listing.Status.TO_PUBLISH, updated_at=timezone.now()
        )
        if count:
            SyncLog.objects.create(message=f"{count} listing prices matched to market")
    return count


def sync_all():
    """Refresh every connected service."""
    services = list(SyncService.objects.all()) or [get_cardtrader()]
    now = timezone.now()
    for service in services:
        service.last_sync = now
        service.save(update_fields=["last_sync"])
        open_conflicts = service.conflicts.filter(resolution=SyncConflict.Resolution.OPEN).count()
        if open_conflicts:
            SyncLog.objects.create(
                level=SyncLog.Level.WARNING,
                message=f"{service.name}: {open_conflicts} listings differ from Ledger",
            )
        else:
            SyncLog.objects.create(message=f"{service.name}: in sync")
    return len(services)


def resolve_conflict(conflict, keep):
    """keep is 'ledger' or 'remote'."""
    listing = getattr(conflict.item, "listing", None)
    with transaction.atomic():
        if keep == SyncConflict.Resolution.REMOTE:
            if listing is not None and conflict.field == "price":
                try:
                    listing.price = Decimal(
                        conflict.remote_value.replace("€", "").replace(",", ".").strip()
                    )
                    listing.save(update_fields=["price", "updated_at"])
                except InvalidOperation:
                    pass
        elif listing is not None and listing.status == Listing.Status.PUBLISHED:
            # Ledger wins: the remote copy must be overwritten on next publish.
            listing.status = Listing.Status.TO_PUBLISH
            listing.save(update_fields=["status", "updated_at"])
        conflict.resolution = keep
        conflict.save(update_fields=["resolution"])
        SyncLog.objects.create(
            message=f"{conflict.service.name}: conflict on {conflict.item.card.name} resolved "
            f"({conflict.get_resolution_display().lower()})"
        )
