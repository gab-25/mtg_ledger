import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import httpx
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import services
from .cardtrader import CardTraderClient, CardTraderError, money
from .models import Card, CollectionItem, Listing, Order, OrderItem, SyncConflict, SyncLog, SyncService


class FakeCardTrader:
    """In-memory stand-in for the CardTrader API, served through httpx.MockTransport."""

    def __init__(self):
        self.expansions = [
            {"id": 10, "game_id": 1, "code": "mh2", "name": "Modern Horizons 2"},
            {"id": 11, "game_id": 1, "code": "neo", "name": "Kamigawa: Neon Dynasty"},
            {"id": 12, "game_id": 1, "code": "dmr", "name": "Dominaria Remastered"},
            {"id": 13, "game_id": 1, "code": "dmu", "name": "Dominaria United"},
            {"id": 99, "game_id": 5, "code": "mh2", "name": "Not MTG"},
        ]
        self.blueprints = {
            10: [{"id": 1001, "name": "Ragavan, Nimble Pilferer", "version": None, "fixed_properties": {"collector_number": "138"}},
                 {"id": 1002, "name": "Solitude", "version": None, "fixed_properties": {"collector_number": "32"}},
                 {"id": 1003, "name": "Urza’s Saga", "version": None, "fixed_properties": {"collector_number": "259"}},
                 {"id": 1004, "name": "Endurance", "version": None, "fixed_properties": {"collector_number": "157"}}],
            11: [{"id": 1101, "name": "Fable of the Mirror-Breaker", "version": "showcase", "fixed_properties": {"collector_number": "356"}},
                 {"id": 1100, "name": "Fable of the Mirror-Breaker", "version": None, "fixed_properties": {"collector_number": "141"}}],
            12: [{"id": 1201, "name": "Counterspell", "version": None, "fixed_properties": {"collector_number": "045"}}],
            13: [{"id": 1301, "name": "Sheoldred, the Apocalypse", "version": None, "fixed_properties": {"collector_number": "107"}}],
        }
        self.products = {}
        self.orders = []
        self.next_product = 5000
        self.jobs = {}
        self.calls = []
        self.fail_blueprints = set()
        self.fail_export = set()  # expansion ids whose /blueprints/export answers 500
        self.fail_marketplace = False

    def handler(self, request):
        path = request.url.path.removeprefix("/api/v2")
        method = request.method
        self.calls.append((method, path))
        body = json.loads(request.content) if request.content else {}
        params = dict(request.url.params)

        if path == "/info":
            return httpx.Response(200, json={"id": 1, "name": "ledger", "user_id": 77})
        if path == "/expansions":
            return httpx.Response(200, json=self.expansions)
        if path == "/blueprints/export":
            if int(params["expansion_id"]) in self.fail_export:
                return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(200, json=self.blueprints.get(int(params["expansion_id"]), []))
        if path == "/products/export":
            return httpx.Response(200, json=list(self.products.values()))
        if path in ("/products/bulk_create", "/products/bulk_update"):
            results = []
            for i, p in enumerate(body["products"]):
                if p.get("blueprint_id") in self.fail_blueprints:
                    results.append({"result": "error", "job_index": i, "errors": {"properties": ["invalid"]}})
                    continue
                pid = p.get("id") or self._new_id()
                prod = self.products.setdefault(pid, {"id": pid, "blueprint_id": p.get("blueprint_id")})
                prod.update(quantity=p["quantity"], price_cents=round(p["price"] * 100),
                            properties_hash=p.get("properties", {}))
                results.append({"result": "ok", "job_index": i, "product_id": pid})
            uuid = f"job-{len(self.jobs)}"
            self.jobs[uuid] = {"uuid": uuid, "state": "completed", "results": results}
            return httpx.Response(200, json={"job": uuid})
        if path.startswith("/jobs/"):
            return httpx.Response(200, json=self.jobs[path.split("/")[-1]])
        if path.startswith("/products/") and method == "DELETE":
            pid = int(path.split("/")[-1])
            if pid not in self.products:
                return httpx.Response(404, json={"error_code": "not_found", "extra": {"message": "gone"}})
            return httpx.Response(200, json={"result": "ok", "resource": self.products.pop(pid)})
        if path == "/marketplace/products" and self.fail_marketplace:
            return httpx.Response(500, json={"error_code": "boom"})
        if path == "/marketplace/products" and "expansion_id" in params:
            def offer(cents, user, cond, lang, foil):
                return {"price": {"cents": cents, "currency": "EUR"}, "user": {"id": user},
                        "properties_hash": {"condition": cond, "mtg_language": lang, "mtg_foil": foil}}
            return httpx.Response(200, json={str(bp["id"]): [
                offer(100, 77, "Near Mint", "it", False),  # our own offer: ignored
                offer(300, 2, "Near Mint", "it", False),
                offer(200, 3, "Slightly Played", "it", False),
                offer(900, 4, "Near Mint", "en", True),
                offer(500, 5, "Near Mint", "tlh", False),  # unknown language: skipped
            ] for bp in self.blueprints.get(int(params["expansion_id"]), [])})
        if path == "/marketplace/products":
            bp = params["blueprint_id"]
            return httpx.Response(200, json={bp: [
                {"price": {"cents": 100, "currency": "EUR"}, "user": {"id": 77},
                 "properties_hash": {"condition": "Near Mint"}},  # our own offer: ignored
                {"price": {"cents": 2240, "currency": "EUR"}, "user": {"id": 2},
                 "properties_hash": {"condition": "Slightly Played"}},
                {"price": {"cents": 2000, "currency": "EUR"}, "user": {"id": 3},
                 "properties_hash": {"condition": "Played"}},
            ]})
        if path == "/orders":
            page = int(params.get("page", 1))
            return httpx.Response(200, json=self.orders if page == 1 else [])
        if path.endswith("/tracking_code"):
            oid = int(path.split("/")[2])
            self._order(oid)["order_shipping_method"]["tracking_code"] = body["tracking_code"]
            return httpx.Response(200, json={})
        if path.endswith("/ship"):
            order = self._order(int(path.split("/")[2]))
            if order["state"] != "paid":
                return httpx.Response(422, json={"error_code": "invalid_state", "extra": {"message": "not paid"}})
            order.update(state="sent", sent_at="2026-11-08T10:00:00.000Z")
            return httpx.Response(200, json=order)
        return httpx.Response(404, json={"error_code": "not_found"})

    def _new_id(self):
        self.next_product += 1
        return self.next_product

    def _order(self, oid):
        return next(o for o in self.orders if o["id"] == oid)

    def client(self, *args, **kwargs):
        return CardTraderClient("test-token", transport=httpx.MockTransport(self.handler), sleep=lambda s: None)


def order_payload(oid, state, items, *, ct0=False, tracked=False):
    return {
        "id": oid, "code": f"code{oid}", "state": state, "via_cardtrader_zero": ct0, "size": sum(i[2] for i in items),
        "buyer": {"username": "buyer1", "email": "b@example.com"},
        "paid_at": "2026-11-07T09:00:00.000Z", "sent_at": None, "packing_number": 4,
        "seller_total": {"cents": 1234, "currency": "EUR"},
        "order_shipping_address": {"name": "Ada", "street": "Via Po 1", "zip": "10100", "city": "Torino",
                                   "state_or_province": "TO", "country": "Italy", "country_code": "IT"},
        "order_shipping_method": {"name": "Letter", "tracked": tracked, "tracking_code": None},
        "order_items": [
            {"id": oid * 10 + n, "product_id": pid, "name": name, "expansion": "MH2", "quantity": qty,
             "seller_price": {"cents": 500, "currency": "EUR"},
             "properties": {"condition": "Near Mint", "mtg_language": "en", "mtg_foil": False}}
            for n, (pid, name, qty) in enumerate(items)
        ],
    }


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


def load_demo(owner):
    """Give owner the demo ledger (the collection shown in the design) shown in the design: collection, listings, conflicts, orders, log."""
    now = timezone.now()
    service = services.get_cardtrader(owner)
    service.last_sync = now - timedelta(days=1)
    service.save()

    items = {}
    for name, type_line, set_code, cond, lang, qty, price, color, listing in CARDS:
        card, _ = Card.objects.get_or_create(
            name=name, set_code=set_code, defaults={"type_line": type_line, "color": color}
        )
        item, _ = CollectionItem.objects.get_or_create(
            owner=owner, card=card, condition=cond, language=lang, foil=False,
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
            "owner": owner,
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

    if not SyncLog.objects.filter(owner=owner).exists():
        SyncLog.objects.create(owner=owner, message="CardTrader: 6 listings published")
        SyncLog.objects.create(owner=owner, level=SyncLog.Level.WARNING, message="CardTrader: 3 prices differ from Ledger")


# Tests never use the real keys from the environment or a local .env file.
@override_settings(OPENROUTER_API_KEY="")
class LedgerTestCase(TestCase):
    def setUp(self):
        cache.clear()
        User = get_user_model()
        self.user = User.objects.create_superuser("gab", password="pw")  # the admin: owns the demo data
        self.other = User.objects.create_user("ami", password="pw")
        load_demo(self.user)
        SyncService.objects.filter(user=self.user).update(api_token="test-token", ct_user_id=77)
        self.client.force_login(self.user)
        self.ct = FakeCardTrader()
        patcher = mock.patch.object(services, "make_client", side_effect=self.ct.client)
        patcher.start()
        self.addCleanup(patcher.stop)
        pause = mock.patch.object(services, "MARKETPLACE_PAUSE", 0)
        pause.start()
        self.addCleanup(pause.stop)

    def publish(self):
        return self.client.post(reverse("ledger:api_publish"))


class PagesTest(LedgerTestCase):
    def test_login_required(self):
        self.client.logout()
        resp = self.client.get(reverse("ledger:collection"))
        self.assertRedirects(resp, f"{reverse('login')}?next=/")

    def test_main_pages_render(self):
        for name in ("collection", "sale", "sync", "shipments", "item_create", "import_csv"):
            with self.subTest(name=name):
                resp = self.client.get(reverse(f"ledger:{name}"))
                self.assertEqual(resp.status_code, 200)
                self.assertContains(resp, "theme/app.css")

    def collection(self, **params):
        resp = self.client.get(reverse("ledger:api_collection"), params)
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def test_collection_shows_stats_and_status(self):
        data = self.collection()
        names = [r["name"] for r in data["results"]]
        self.assertIn("Ragavan, Nimble Pilferer", names)
        self.assertIn("Sync pending", [r["badge"]["label"] for r in data["results"]])
        self.assertEqual(data["stats"]["total"], 21)
        self.assertFalse(data["filtered"])
        counts = self.client.get(reverse("accounts:api_me")).json()["counts"]
        self.assertEqual(counts, {"pending_changes": 3, "orders_to_ship": 2})

    def test_collection_filters(self):
        data = self.collection(color="B", q="seize")
        self.assertEqual([r["name"] for r in data["results"]], ["Thoughtseize"])
        self.assertTrue(data["filtered"])
        self.assertEqual(len(self.collection(set="LTR", cond="NM")["results"]), 2)

    def test_collection_filter_choices(self):
        data = self.collection()
        self.assertEqual(data["sets"], sorted(set(data["sets"])))
        self.assertEqual([c["code"] for c in data["color_filters"]], ["W", "U", "B", "R", "G"])
        self.assertTrue(all(c["pip"].startswith("#") for c in data["color_filters"]))

    def test_pagination_payload(self):
        card = Card.objects.create(name="Island", set_code="ZZZ")
        CollectionItem.objects.bulk_create(
            [CollectionItem(owner=self.user, card=card, condition="NM", language="EN", quantity=1, price=i)
             for i in range(60)]
        )
        data = self.collection(set="ZZZ")
        self.assertEqual((data["count"], data["page"], data["num_pages"], len(data["results"])), (60, 1, 2, 50))
        data = self.collection(set="ZZZ", page="99")
        self.assertEqual((data["page"], len(data["results"])), (2, 10))

    def test_choices(self):
        data = self.client.get(reverse("ledger:api_choices")).json()
        self.assertIn(["NM", "Near Mint"], data["conditions"])
        self.assertIn(["R", "Red"], data["colors"])
        self.assertIn(["EN", "English"], data["languages"])
        self.assertIn(["to_publish", "To publish"], data["listing_statuses"])


BOLT = {"name": "Lightning Bolt", "set_code": "m10", "type_line": "Instant", "color": "R",
        "condition": "NM", "language": "EN", "quantity": 4, "price": "1.50"}


class ItemTest(LedgerTestCase):
    def test_add_item_creates_card(self):
        resp = self.client.post(reverse("ledger:api_collection"), BOLT, content_type="application/json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["messages"], [{"level": "success", "text": "Added 4× Lightning Bolt."}])
        item = CollectionItem.objects.get(card__name="Lightning Bolt")
        self.assertEqual(resp.json()["id"], item.pk)
        self.assertEqual(item.card.set_code, "M10")
        self.assertEqual(item.quantity, 4)

    def test_add_item_validation_error(self):
        resp = self.client.post(reverse("ledger:api_collection"), {**BOLT, "name": ""}, content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("name", resp.json())

    def test_edit_and_delete_item(self):
        item = CollectionItem.objects.filter(owner=self.user, card__name="Thoughtseize").first()
        url = reverse("ledger:api_item", args=[item.pk])
        data = self.client.get(url).json()
        self.assertEqual((data["name"], data["set_code"], data["quantity"]), ("Thoughtseize", "2XM", item.quantity))
        self.assertIn("has_listing", data)
        resp = self.client.put(url, {**data, "quantity": 9, "foil": True}, content_type="application/json")
        self.assertEqual(resp.json()["messages"][0]["text"], "Saved Thoughtseize.")
        item.refresh_from_db()
        self.assertEqual((item.quantity, item.foil), (9, True))
        resp = self.client.delete(url)
        self.assertEqual(resp.json()["messages"][0]["text"], "Removed Thoughtseize from the collection.")
        self.assertFalse(CollectionItem.objects.filter(pk=item.pk).exists())


class CsvTest(LedgerTestCase):
    def test_export_then_import_roundtrip(self):
        resp = self.client.get(reverse("ledger:api_export"))
        self.assertTrue(resp["Content-Disposition"].startswith('attachment; filename="mtg-ledger-'))
        csv_bytes = resp.content
        self.assertIn(b"name,set,type", csv_bytes)
        before = CollectionItem.objects.count()
        upload = SimpleUploadedFile("c.csv", csv_bytes + "Lightning Bolt,M10,Instant,R,NM,EN,0,4,1.50\n".encode())
        resp = self.client.post(reverse("ledger:api_import"), {"file": upload})
        self.assertEqual(resp.status_code, 200)
        self.assertRegex(resp.json()["messages"][0]["text"], r"^Import complete: 1 new, \d+ updated\.$")
        self.assertEqual(CollectionItem.objects.count(), before + 1)

    def test_import_columns(self):
        self.assertEqual(self.client.get(reverse("ledger:api_import")).json()["columns"], list(services.CSV_FIELDS))

    def test_import_rejects_bad_rows(self):
        upload = SimpleUploadedFile("c.csv", b"name,set,condition\nBolt,M10,XX\n")
        resp = self.client.post(reverse("ledger:api_import"), {"file": upload})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("invalid condition", resp.json()["file"][0])
        self.assertFalse(CollectionItem.objects.filter(card__name="Bolt").exists())


class CollectionIsolationTest(LedgerTestCase):
    """load_demo gave the demo collection to self.user; self.other is a friend with nothing yet."""

    def as_other(self):
        self.client.force_login(self.other)

    def test_friend_sees_empty_collection(self):
        self.as_other()
        data = self.client.get(reverse("ledger:api_collection")).json()
        self.assertFalse(data["stats"]["total"])
        self.assertEqual(data["stats"]["unique"], 0)
        self.assertEqual(data["results"], [])
        self.assertEqual(data["sets"], [])
        self.assertEqual(self.client.get(reverse("accounts:api_me")).json()["counts"],
                         {"pending_changes": 0, "orders_to_ship": 0})

    def test_item_of_other_user_is_404(self):
        item = CollectionItem.objects.filter(owner=self.user).first()
        self.as_other()
        url = reverse("ledger:api_item", args=[item.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.put(url, {}, content_type="application/json").status_code, 404)
        self.assertEqual(self.client.delete(url).status_code, 404)
        self.assertTrue(CollectionItem.objects.filter(pk=item.pk).exists())

    def test_csv_import_creates_own_item_for_same_printing(self):
        mine = CollectionItem.objects.get(owner=self.user, card__name="Thoughtseize")
        self.as_other()
        upload = SimpleUploadedFile("c.csv", b"name,set,condition,language,quantity,price\nThoughtseize,2XM,NM,EN,7,9.00\n")
        resp = self.client.post(reverse("ledger:api_import"), {"file": upload})
        self.assertEqual(resp.status_code, 200)
        theirs = CollectionItem.objects.get(owner=self.other)
        self.assertEqual((theirs.card_id, theirs.quantity), (mine.card_id, 7))
        mine.refresh_from_db()
        self.assertEqual(mine.quantity, 3)
        self.assertTrue(SyncLog.objects.filter(owner=self.other, message__startswith="CSV import").exists())

    def test_add_cards_form_creates_own_item(self):
        self.as_other()
        self.client.post(reverse("ledger:api_collection"), {
            "name": "Thoughtseize", "set_code": "2XM", "color": "B",
            "condition": "NM", "language": "EN", "quantity": 1, "price": "12.00",
        }, content_type="application/json")
        self.assertEqual(CollectionItem.objects.get(owner=self.other).card.name, "Thoughtseize")
        self.assertEqual(CollectionItem.objects.get(owner=self.user, card__name="Thoughtseize").quantity, 3)

    def test_export_contains_only_own_rows(self):
        self.as_other()
        self.client.post(reverse("ledger:api_collection"), {**BOLT, "quantity": 1}, content_type="application/json")
        csv_text = self.client.get(reverse("ledger:api_export")).content.decode()
        self.assertIn("Lightning Bolt", csv_text)
        self.assertNotIn("Ragavan", csv_text)


class ClientTest(TestCase):
    def test_errors_are_wrapped(self):
        def handler(request):
            return httpx.Response(401, json={"error_code": "unauthorized", "extra": {"message": "bad token"}})
        client = CardTraderClient("x", transport=httpx.MockTransport(handler))
        with self.assertRaisesMessage(CardTraderError, "bad token"):
            client.info()

    def test_rate_limit(self):
        client = CardTraderClient("x", transport=httpx.MockTransport(lambda r: httpx.Response(429, json={})))
        with self.assertRaises(CardTraderError) as ctx:
            client.expansions()
        self.assertEqual(ctx.exception.status, 429)

    def test_bearer_header_and_failed_job(self):
        seen = {}

        def handler(request):
            seen["auth"] = request.headers["Authorization"]
            if request.url.path.endswith("bulk_create"):
                return httpx.Response(200, json={"job": "u1"})
            return httpx.Response(200, json={"uuid": "u1", "state": "unprocessable"})
        client = CardTraderClient("tok", transport=httpx.MockTransport(handler), sleep=lambda s: None)
        with self.assertRaises(CardTraderError):
            client.bulk_create([{"blueprint_id": 1, "price": 1.0, "quantity": 1}])
        self.assertEqual(seen["auth"], "Bearer tok")

    def test_missing_token(self):
        with self.assertRaises(CardTraderError):
            CardTraderClient("")

    def test_money(self):
        self.assertEqual(money({"cents": 6258, "currency": "EUR"}), Decimal("62.58"))
        self.assertEqual(money(5), Decimal("0.05"))
        self.assertIsNone(money(None))


class PublishTest(LedgerTestCase):
    def test_publish_creates_products_with_blueprints(self):
        self.publish()
        self.assertFalse(Listing.objects.filter(status=Listing.Status.TO_PUBLISH).exists())
        fable = Listing.objects.get(item__card__name="Fable of the Mirror-Breaker")
        self.assertIsNotNone(fable.ct_product_id)
        self.assertEqual(fable.item.card.ct_blueprint_id, 1100)  # base version, not showcase
        product = self.ct.products[fable.ct_product_id]
        self.assertEqual(product["price_cents"], 1920)
        self.assertEqual(product["properties_hash"], {"condition": "Near Mint", "mtg_language": "jp"})
        sheoldred = Listing.objects.get(item__card__name="Sheoldred, the Apocalypse")
        self.assertEqual(self.ct.products[sheoldred.ct_product_id]["properties_hash"]["condition"], "Played")
        self.assertTrue(SyncLog.objects.filter(message__contains="3 listings published").exists())

    def test_publish_updates_existing_product(self):
        self.publish()
        listing = Listing.objects.get(item__card__name="Counterspell")
        pid = listing.ct_product_id
        resp = self.client.put(reverse("ledger:api_listing", args=[listing.item.pk]), {
            "quantity": 2, "price": "0.95", "market_price": "0.70", "status": "published",
        }, content_type="application/json")
        self.assertEqual(resp.json()["messages"][0]["text"], "Listing for Counterspell saved.")
        self.publish()
        listing.refresh_from_db()
        self.assertEqual(listing.ct_product_id, pid)
        self.assertEqual(self.ct.products[pid]["price_cents"], 95)
        self.assertEqual(self.ct.products[pid]["quantity"], 2)

    def test_unmatched_and_rejected_listings_stay_pending(self):
        card = Card.objects.get(name="Counterspell")
        card.name = "Counterspel"  # typo: not in the catalog
        card.save()
        self.ct.fail_blueprints = {1301}
        resp = self.publish()
        self.assertEqual(
            set(Listing.objects.filter(status=Listing.Status.TO_PUBLISH).values_list("item__card__name", flat=True)),
            {"Counterspel", "Sheoldred, the Apocalypse"},
        )
        messages = resp.json()["messages"]
        self.assertEqual(messages[0], {"level": "success", "text": "Published 1 listing to CardTrader."})
        warnings = [m["text"] for m in messages if m["level"] == "warning"]
        self.assertEqual(len(warnings), 2)
        self.assertTrue(any(w.startswith("Counterspel: ") and "not found in the CardTrader catalog" in w
                            for w in warnings))

    def test_api_error_is_shown(self):
        with mock.patch.object(services, "make_client", side_effect=CardTraderError("no token")):
            resp = self.publish()
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.json(), {"detail": "Publish failed: no token", "code": "service_error"})

    def test_delete_listing_removes_remote_product(self):
        self.publish()
        listing = Listing.objects.get(item__card__name="Counterspell")
        pid = listing.ct_product_id
        resp = self.client.delete(reverse("ledger:api_listing", args=[listing.item.pk]))
        self.assertEqual(resp.json()["messages"], [{"level": "success", "text": "Listing removed."}])
        self.assertNotIn(pid, self.ct.products)
        self.assertFalse(Listing.objects.filter(pk=listing.pk).exists())

    def test_delete_listing_error_keeps_it(self):
        self.publish()
        listing = Listing.objects.get(item__card__name="Counterspell")
        with mock.patch.object(services, "remove_listing", side_effect=CardTraderError("down")):
            resp = self.client.delete(reverse("ledger:api_listing", args=[listing.item.pk]))
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.json()["detail"], "Listing not removed: down")
        self.assertTrue(Listing.objects.filter(pk=listing.pk).exists())


class SaleApiTest(LedgerTestCase):
    def listing(self, name="Counterspell"):
        return Listing.objects.select_related("item__card").get(item__card__name=name)

    def put(self, item_pk, **data):
        return self.client.put(reverse("ledger:api_listing", args=[item_pk]), data, content_type="application/json")

    def test_listings_stats_and_filter(self):
        data = self.client.get(reverse("ledger:api_listings")).json()
        self.assertEqual(set(data["stats"]), {"active", "value", "sold_month", "to_publish"})
        self.assertEqual(data["stats"]["to_publish"], 3)
        self.assertEqual(data["status"], "")
        row = next(r for r in data["results"] if r["name"] == "Counterspell")
        self.assertEqual(set(row), {"item_id", "name", "set_code", "condition", "language", "foil", "quantity",
                                    "price", "market_price", "pip", "badge"})
        sold = self.client.get(reverse("ledger:api_listings"), {"status": "sold"}).json()
        self.assertEqual(sold["status"], "sold")
        self.assertEqual({r["badge"]["label"] for r in sold["results"]}, {"Sold"})

    def test_edit_published_listing_requeues(self):
        self.publish()
        listing = self.listing()
        self.assertEqual(listing.status, Listing.Status.PUBLISHED)
        self.put(listing.item_id, quantity=listing.quantity, price="0.99", market_price="", status="published")
        listing.refresh_from_db()
        self.assertEqual((listing.price, listing.status), (Decimal("0.99"), Listing.Status.TO_PUBLISH))

    def test_mark_sold_sets_sold_at(self):
        listing = self.listing()
        self.put(listing.item_id, quantity=1, price="1.00", market_price="", status="sold")
        listing.refresh_from_db()
        self.assertIsNotNone(listing.sold_at)

    def test_quantity_over_owned(self):
        listing = self.listing()
        owned = listing.item.quantity
        resp = self.put(listing.item_id, quantity=owned + 1, price="1.00", market_price="", status="to_publish")
        self.assertEqual(resp.status_code, 400)
        copies = "copy" if owned == 1 else "copies"
        self.assertEqual(resp.json(), {"quantity": [f"You only own {owned} {copies}."]})

    def test_new_listing_defaults(self):
        item = CollectionItem.objects.filter(owner=self.user, listing__isnull=True).select_related("card").first()
        data = self.client.get(reverse("ledger:api_listing", args=[item.pk])).json()
        self.assertEqual(data["item"]["name"], item.card.name)
        self.assertEqual(data["item"]["quantity"], item.quantity)
        self.assertEqual(data["listing"], {"exists": False, "quantity": 1, "price": f"{item.price:.2f}",
                                           "market_price": None, "status": "to_publish"})
        resp = self.put(item.pk, quantity=1, price="2.00", market_price="", status="to_publish")
        self.assertEqual(resp.json()["messages"][0]["text"], f"Listing for {item.card.name} saved.")
        self.assertTrue(self.client.get(reverse("ledger:api_listing", args=[item.pk])).json()["listing"]["exists"])

    def test_match_prices_message(self):
        resp = self.client.post(reverse("ledger:api_match_prices"))
        self.assertRegex(resp.json()["messages"][0]["text"], r"^\d+ prices matched to market\.$")
        resp = self.client.post(reverse("ledger:api_match_prices"))
        self.assertEqual(resp.json()["messages"][0]["text"], "All prices already match the market.")


class MarketTest(LedgerTestCase):
    def test_match_market_prices_uses_same_condition_and_ignores_own_offers(self):
        self.client.post(reverse("ledger:api_match_prices"))
        solitude = Listing.objects.get(item__card__name="Solitude")  # EX -> Slightly Played
        self.assertEqual(solitude.market_price, Decimal("22.40"))
        self.assertEqual(solitude.price, Decimal("22.40"))
        self.assertEqual(solitude.status, Listing.Status.TO_PUBLISH)
        ragavan = Listing.objects.get(item__card__name="Ragavan, Nimble Pilferer")  # NM: no other NM offer
        self.assertEqual(ragavan.market_price, Decimal("20.00"))
        sold = Listing.objects.get(item__card__name="Endurance")
        self.assertEqual(sold.price, Decimal("11.00"))


class SyncTest(LedgerTestCase):
    def test_sync_detects_remote_differences(self):
        SyncConflict.objects.all().delete()
        self.publish()
        counterspell = Listing.objects.get(item__card__name="Counterspell")
        self.ct.products[counterspell.ct_product_id]["price_cents"] = 70
        fable = Listing.objects.get(item__card__name="Fable of the Mirror-Breaker")
        del self.ct.products[fable.ct_product_id]
        self.client.post(reverse("ledger:sync_all"))
        conflicts = {c.item.card.name: c for c in SyncConflict.objects.filter(resolution="open")}
        self.assertEqual(set(conflicts), {"Counterspell", "Fable of the Mirror-Breaker"})
        self.assertEqual(conflicts["Counterspell"].remote_amount, Decimal("0.70"))
        # A second sync does not duplicate conflicts, and fixed ones disappear.
        self.ct.products[counterspell.ct_product_id]["price_cents"] = 80
        self.client.post(reverse("ledger:sync_all"))
        self.assertEqual(
            list(SyncConflict.objects.filter(resolution="open").values_list("item__card__name", flat=True)),
            ["Fable of the Mirror-Breaker"],
        )

    def test_keep_remote_price(self):
        conflict = SyncConflict.objects.get(field="price")
        self.client.post(reverse("ledger:resolve", args=[conflict.pk]), {"keep": "remote"})
        conflict.refresh_from_db()
        self.assertEqual(conflict.resolution, "remote")
        self.assertEqual(conflict.item.listing.price, Decimal("0.70"))

    def test_keep_ledger_requeues(self):
        conflict = SyncConflict.objects.get(field="status")
        listing = conflict.item.listing
        listing.ct_product_id = 42
        listing.save()
        self.client.post(reverse("ledger:resolve", args=[conflict.pk]), {"keep": "ledger"})
        listing.refresh_from_db()
        self.assertEqual(listing.status, Listing.Status.TO_PUBLISH)
        self.assertIsNone(listing.ct_product_id)

    def test_sync_page(self):
        resp = self.client.get(reverse("ledger:sync"))
        self.assertEqual(len(resp.context["conflicts"]), 3)
        self.assertContains(resp, "Configured")
        self.client.force_login(self.other)
        self.assertContains(self.client.get(reverse("ledger:sync")), "Not configured")


class ShipmentsTest(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.publish()
        self.ragavan = Listing.objects.get(item__card__name="Ragavan, Nimble Pilferer")
        self.sheoldred = Listing.objects.get(item__card__name="Sheoldred, the Apocalypse")
        self.ragavan.ct_product_id = 777
        self.ragavan.save()

    def import_orders(self):
        return self.client.post(reverse("ledger:orders_sync"))

    def test_import_orders_decrements_stock_once(self):
        self.ct.orders = [order_payload(1, "paid", [(self.sheoldred.ct_product_id, "Sheoldred", 1)])]
        self.import_orders()
        self.import_orders()  # idempotent
        self.sheoldred.refresh_from_db()
        self.assertEqual(self.sheoldred.quantity, 1)
        self.assertEqual(self.sheoldred.item.quantity, 1)
        order = Order.objects.get(ct_id=1)
        self.assertEqual(order.items.get().listing, self.sheoldred)
        self.assertEqual(order.total, Decimal("12.34"))
        self.assertEqual(order.address_lines, ["Ada", "Via Po 1", "10100 Torino TO", "Italy"])

    def test_sold_out_listing_becomes_sold(self):
        self.ct.orders = [order_payload(2, "paid", [(777, "Ragavan", 1)])]
        self.import_orders()
        self.ragavan.refresh_from_db()
        self.assertEqual(self.ragavan.status, Listing.Status.SOLD)
        self.assertIsNotNone(self.ragavan.sold_at)

    def test_ct_zero_rules(self):
        # hub_pending decrements; the weekly merged order (paid, via CT0) does not.
        self.ct.orders = [
            order_payload(3, "hub_pending", [(777, "Ragavan", 1)], ct0=True),
            order_payload(4, "paid", [(777, "Ragavan", 1)], ct0=True),
        ]
        self.import_orders()
        self.ragavan.refresh_from_db()
        self.assertEqual(self.ragavan.item.quantity, 0)
        self.assertTrue(Order.objects.get(ct_id=3).stock_applied)
        self.assertFalse(Order.objects.get(ct_id=4).stock_applied)

    def test_ship_with_tracking(self):
        self.ct.orders = [order_payload(5, "paid", [(777, "Ragavan", 1)], tracked=True)]
        self.import_orders()
        order = Order.objects.get(ct_id=5)
        resp = self.client.post(reverse("ledger:order_ship", args=[order.pk]), {"tracking_code": ""}, follow=True)
        self.assertContains(resp, "need a tracking code")
        resp = self.client.post(reverse("ledger:order_ship", args=[order.pk]), {"tracking_code": "RR1IT"})
        self.assertRedirects(resp, reverse("ledger:shipments"))
        order.refresh_from_db()
        self.assertEqual(order.state, Order.State.SENT)
        self.assertEqual(order.tracking_code, "RR1IT")
        self.assertIsNotNone(order.sent_at)
        self.assertIn(("PUT", "/orders/5/tracking_code"), self.ct.calls)

    def test_cannot_ship_twice(self):
        order = Order.objects.get(state="sent")
        resp = self.client.post(reverse("ledger:order_ship", args=[order.pk]), {"tracking_code": "X"}, follow=True)
        self.assertContains(resp, "cannot be shipped")

    def test_shipments_tabs(self):
        resp = self.client.get(reverse("ledger:shipments"))
        self.assertEqual(len(resp.context["rows"]), 2)
        self.assertEqual(resp.context["stats"]["items_to_ship"], 3)
        resp = self.client.get(reverse("ledger:shipments"), {"tab": "ct_zero"})
        self.assertEqual([r["o"].state for r in resp.context["rows"]], ["hub_pending"])
        order = Order.objects.filter(state="paid").first()
        resp = self.client.get(reverse("ledger:order_detail", args=[order.pk]))
        self.assertContains(resp, "Mark as shipped")
        self.assertContains(resp, "Packing list")


CONNECT = "Connect your CardTrader token in Profile and settings to sell."


class SellingIsolationTest(LedgerTestCase):
    """self.user (the admin) sells with their own token; self.other is a friend."""

    def friend_listing(self, status=Listing.Status.TO_PUBLISH, ct_product_id=None, name="Lightning Bolt"):
        card = Card.objects.create(name=name, set_code="M10", ct_blueprint_id=1001 if name == "Lightning Bolt" else None)
        item = CollectionItem.objects.create(owner=self.other, card=card, quantity=2, price=Decimal("1.00"))
        return Listing.objects.create(item=item, quantity=1, price=Decimal("1.00"), status=status,
                                      ct_product_id=ct_product_id)

    def give_friend_token(self):
        service = services.get_cardtrader(self.other)
        service.api_token, service.ct_user_id = "friend-token", 78
        service.save()

    def messages(self, resp):
        return [str(m) for m in resp.wsgi_request._messages]

    def test_friend_without_token_cannot_sell(self):
        self.friend_listing()
        order = Order.objects.create(owner=self.other, ct_id=99, code="c99", state=Order.State.PAID)
        self.client.force_login(self.other)
        self.ct.calls.clear()
        for name, args in (("publish", []), ("sync_all", []), ("orders_sync", []), ("match_prices", []),
                           ("order_ship", [order.pk])):
            with self.subTest(name=name):
                resp = self.client.post(reverse(f"ledger:{name}", args=args), {"tracking_code": "X"}, follow=True)
                self.assertTrue(any(CONNECT in m for m in self.messages(resp)), self.messages(resp))
        self.assertEqual(self.ct.calls, [])

    def test_publish_only_pushes_own_listings(self):
        self.give_friend_token()
        self.friend_listing()
        mine_before = Listing.objects.owned_by(self.user).filter(status=Listing.Status.TO_PUBLISH).count()
        result = services.publish_pending(self.other)
        self.assertEqual(result.published, 1)
        self.assertEqual(Listing.objects.owned_by(self.other).get().status, Listing.Status.PUBLISHED)
        self.assertEqual(
            Listing.objects.owned_by(self.user).filter(status=Listing.Status.TO_PUBLISH).count(), mine_before,
        )
        self.assertTrue(SyncLog.objects.filter(owner=self.other, message__contains="listings published").exists())

    def test_imported_orders_belong_to_the_importer_and_only_touch_own_stock(self):
        self.give_friend_token()
        theirs = self.friend_listing(status=Listing.Status.PUBLISHED, ct_product_id=5001)
        mine = Listing.objects.owned_by(self.user).get(item__card__name="Sheoldred, the Apocalypse")
        mine.ct_product_id, mine.status = 5002, Listing.Status.PUBLISHED
        mine.save()
        self.ct.orders = [order_payload(50, "paid", [(5001, "Lightning Bolt", 1), (5002, "Sheoldred", 1)])]
        services.sync_all(self.other)
        order = Order.objects.get(ct_id=50)
        self.assertEqual(order.owner, self.other)
        self.assertEqual(order.items.get(ct_product_id=5001).listing, theirs)
        self.assertIsNone(order.items.get(ct_product_id=5002).listing)
        mine.refresh_from_db()
        self.assertEqual((mine.quantity, mine.item.quantity), (2, 2))
        theirs.refresh_from_db()
        self.assertEqual(theirs.item.quantity, 1)

    def test_sync_does_not_touch_other_users_conflicts(self):
        self.give_friend_token()
        services.sync_all(self.other)
        self.assertEqual(SyncConflict.objects.owned_by(self.user).filter(resolution="open").count(), 3)

    def test_other_users_objects_are_404(self):
        listing = Listing.objects.owned_by(self.user).first()
        conflict = SyncConflict.objects.owned_by(self.user).first()
        order = Order.objects.owned_by(self.user).filter(state=Order.State.PAID).first()
        self.client.force_login(self.other)
        for method, name, args in (
            ("get", "listing_edit", [listing.item.pk]), ("post", "listing_delete", [listing.item.pk]),
            ("post", "resolve", [conflict.pk]), ("get", "order_detail", [order.pk]),
            ("post", "order_ship", [order.pk]),
        ):
            with self.subTest(name=name):
                resp = getattr(self.client, method)(reverse(f"ledger:{name}", args=args), {"keep": "remote"})
                self.assertEqual(resp.status_code, 404)

    def test_friend_pages_show_only_their_data(self):
        SyncLog.objects.create(message="Global note")
        self.client.force_login(self.other)
        resp = self.client.get(reverse("ledger:sync"))
        self.assertContains(resp, "Global note")
        self.assertNotContains(resp, "6 listings published")
        self.assertEqual(len(resp.context["conflicts"]), 0)
        self.assertEqual(self.client.get(reverse("ledger:sale")).context["stats"]["active"], 0)
        self.assertEqual(self.client.get(reverse("ledger:shipments")).context["rows"], [])
