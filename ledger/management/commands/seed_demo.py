from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from ledger.models import Card, CollectionItem, Listing, Order, OrderItem, SyncConflict, SyncLog
from ledger.ownership import default_owner
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

# name, field, Ledger value, CardTrader value, numeric CardTrader value
CONFLICTS = [
    ("Sheoldred, the Apocalypse", "quantity", "2 for sale", "1 for sale", Decimal("1")),
    ("Counterspell", "price", "€0.80", "€0.70", Decimal("0.70")),
    ("Fable of the Mirror-Breaker", "status", "Published", "Not on CardTrader", None),
]


class Command(BaseCommand):
    help = "Load the demo collection shown in the design (wipes existing ledger data with --reset)."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete all ledger data first")

    @transaction.atomic
    def handle(self, *args, reset=False, **options):
        if reset:
            Card.objects.all().delete()
            Order.objects.all().delete()
            SyncLog.objects.all().delete()
        now = timezone.now()
        service = get_cardtrader(default_owner())
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

        for name, field, local, remote, amount in CONFLICTS:
            SyncConflict.objects.get_or_create(
                service=service, item=items[name], field=field,
                defaults={"local_value": local, "remote_value": remote, "remote_amount": amount},
            )

        # Demo shipments (already counted in the stock above).
        orders = [
            (9000001, "20261107a1b2c3", "paid", False, "mtgfan88", ("Luca Bianchi", "Via Roma 12", "20121", "Milano", "MI", "Italy", "IT"), "Raccomandata", True, 1, [("Endurance", "Modern Horizons 2", 1, "11.00", "Played")]),
            (9000002, "20261107d4e5f6", "paid", False, "proxyless", ("Marie Dubois", "12 rue des Lilas", "69003", "Lyon", "", "France", "FR"), "Priority Letter", False, 2, [("Counterspell", "Dominaria Remastered", 2, "0.80", "Near Mint")]),
            (9000003, "20261106g7h8i9", "hub_pending", True, "CT Connect", None, "", False, None, [("Solitude", "Modern Horizons 2", 1, "21.00", "Slightly Played")]),
            (9000004, "20261102j1k2l3", "sent", False, "budgetbrewer", ("Jonas Weber", "Hauptstr. 5", "10115", "Berlin", "", "Germany", "DE"), "Tracked Letter", True, None, [("Thoughtseize", "Double Masters", 1, "12.00", "Near Mint")]),
        ]
        for ct_id, code, state, ct0, buyer, addr, method, tracked, packing, lines in orders:
            name, street, zip_code, city, prov, country, cc = addr or ("", "", "", "", "", "", "")
            order, _ = Order.objects.update_or_create(ct_id=ct_id, defaults={
                "code": code, "state": state, "via_cardtrader_zero": ct0, "buyer_username": buyer,
                "ship_name": name, "ship_street": street, "ship_zip": zip_code, "ship_city": city,
                "ship_province": prov, "ship_country": country, "ship_country_code": cc,
                "shipping_method": method, "tracked": tracked, "packing_number": packing,
                "size": sum(l[2] for l in lines), "currency": "EUR",
                "total": sum(Decimal(l[3]) * l[2] for l in lines),
                "paid_at": now - timedelta(days=1 if state != "sent" else 6),
                "sent_at": now - timedelta(days=5) if state == "sent" else None,
                "tracking_code": "RR123456789IT" if state == "sent" else "",
                "stock_applied": True,
            })
            for i, (card_name, expansion, qty, price, cond) in enumerate(lines, start=1):
                OrderItem.objects.update_or_create(order=order, ct_id=ct_id * 10 + i, defaults={
                    "name": card_name, "expansion": expansion, "quantity": qty,
                    "price": Decimal(price), "condition": cond, "language": "EN",
                })

        if not SyncLog.objects.exists():
            SyncLog.objects.create(message="CardTrader: 6 listings published")
            SyncLog.objects.create(level=SyncLog.Level.WARNING, message="CardTrader: 3 prices differ from Ledger")
        self.stdout.write(self.style.SUCCESS(f"Seeded {len(items)} collection entries."))
