from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from ledger.models import Card, CollectionItem, Listing, SyncConflict, SyncLog
from ledger.services import get_cardtrader

# name, type, set, cond, lang, qty, price, color, listing (status, price, market) or None
CARDS = [
    ("Ragavan, Nimble Pilferer", "Creature — Monkey Pirate", "MH2", "NM", "EN", 1, "48.00", "R", ("published", "48.00", "46.50")),
    ("The One Ring", "Legendary Artifact", "LTR", "NM", "EN", 1, "62.50", "C", None),
    ("Sheoldred, the Apocalypse", "Legendary Creature — Phyrexian Praetor", "DMU", "LP", "IT", 2, "71.80", "B", ("to_publish", "71.80", "70.00")),
    ("Orcish Bowmasters", "Creature — Orc Archer", "LTR", "NM", "EN", 4, "23.50", "B", None),
    ("Fable of the Mirror-Breaker", "Enchantment — Saga", "NEO", "NM", "JP", 2, "19.20", "R", ("to_publish", "19.20", "18.90")),
    ("Wrenn and Six", "Planeswalker — Wrenn", "2XM", "EX", "EN", 1, "29.90", "M", None),
    ("Counterspell", "Instant", "DMR", "NM", "IT", 4, "0.80", "U", ("to_publish", "0.80", "0.70")),
    ("Thoughtseize", "Sorcery", "2XM", "NM", "EN", 3, "12.00", "B", None),
    ("Solitude", "Creature — Elemental Incarnation", "MH2", "EX", "EN", 1, "21.00", "W", ("published", "21.00", "22.40")),
    ("Urza’s Saga", "Enchantment Land — Urza’s Saga", "MH2", "NM", "EN", 1, "24.50", "C", ("published", "24.50", "24.00")),
    ("Endurance", "Creature — Reptile Elemental Incarnation", "MH2", "LP", "EN", 1, "11.00", "G", ("sold", "11.00", "11.30")),
]

CONFLICTS = [
    ("Sheoldred, the Apocalypse", "quantity", "2 for sale", "1 for sale, 1 sold"),
    ("Counterspell", "price", "€0.80", "€0.70"),
    ("Fable of the Mirror-Breaker", "status", "For sale", "Not published"),
]


class Command(BaseCommand):
    help = "Load the demo collection shown in the design (wipes existing ledger data with --reset)."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete all ledger data first")

    @transaction.atomic
    def handle(self, *args, reset=False, **options):
        if reset:
            Card.objects.all().delete()
            SyncLog.objects.all().delete()
        now = timezone.now()
        service = get_cardtrader()
        service.last_sync = now - timedelta(days=1)
        service.save()

        items = {}
        for name, type_line, set_code, cond, lang, qty, price, color, listing in CARDS:
            card, _ = Card.objects.get_or_create(
                name=name, set_code=set_code, defaults={"type_line": type_line, "color": color}
            )
            item, _ = CollectionItem.objects.get_or_create(
                card=card, condition=cond, language=lang, foil=False,
                defaults={"quantity": qty, "price": Decimal(price)},
            )
            items[name] = item
            if listing:
                status, lprice, market = listing
                Listing.objects.update_or_create(item=item, defaults={
                    "price": Decimal(lprice), "market_price": Decimal(market), "status": status,
                    "quantity": qty if name == "Sheoldred, the Apocalypse" else 1,
                    "sold_at": now if status == "sold" else None,
                })

        for name, field, local, remote in CONFLICTS:
            SyncConflict.objects.get_or_create(
                service=service, item=items[name], field=field,
                defaults={"local_value": local, "remote_value": remote},
            )

        if not SyncLog.objects.exists():
            SyncLog.objects.create(message="CardTrader: 6 listings published")
            SyncLog.objects.create(level=SyncLog.Level.WARNING, message="CardTrader: 3 prices differ from Ledger")
        self.stdout.write(self.style.SUCCESS(f"Seeded {len(items)} collection entries."))
