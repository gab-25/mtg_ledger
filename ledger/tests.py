import json
from decimal import Decimal
from io import StringIO
from unittest import mock

import httpx
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

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


# Tests never use the real keys from the environment or a local .env file.
@override_settings(OPENROUTER_API_KEY="")
class LedgerTestCase(TestCase):
    def setUp(self):
        cache.clear()
        User = get_user_model()
        self.user = User.objects.create_superuser("gab", password="pw")  # the admin: owns the demo data
        self.other = User.objects.create_user("ami", password="pw")
        call_command("seed_demo", "--reset", "--user", "gab", stdout=StringIO())
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
        return self.client.post(reverse("ledger:publish"))


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

    def test_collection_shows_stats_and_status(self):
        resp = self.client.get(reverse("ledger:collection"))
        self.assertContains(resp, "Ragavan, Nimble Pilferer")
        self.assertContains(resp, "Sync pending")
        self.assertEqual(resp.context["pending_changes"], 3)
        self.assertEqual(resp.context["orders_to_ship"], 2)
        self.assertEqual(resp.context["stats"]["total"], 21)

    def test_collection_filters(self):
        resp = self.client.get(reverse("ledger:collection"), {"color": "B", "q": "seize"})
        self.assertEqual([r["item"].card.name for r in resp.context["rows"]], ["Thoughtseize"])
        resp = self.client.get(reverse("ledger:collection"), {"set": "LTR", "cond": "NM"})
        self.assertEqual(len(resp.context["rows"]), 2)


class ItemTest(LedgerTestCase):
    def test_add_item_creates_card(self):
        resp = self.client.post(reverse("ledger:item_create"), {
            "name": "Lightning Bolt", "set_code": "m10", "type_line": "Instant", "color": "R",
            "condition": "NM", "language": "EN", "quantity": 4, "price": "1.50",
        })
        self.assertRedirects(resp, reverse("ledger:collection"))
        item = CollectionItem.objects.get(card__name="Lightning Bolt")
        self.assertEqual(item.card.set_code, "M10")
        self.assertEqual(item.quantity, 4)


class CsvTest(LedgerTestCase):
    def test_export_then_import_roundtrip(self):
        csv_bytes = self.client.get(reverse("ledger:export_csv")).content
        self.assertIn(b"name,set,type", csv_bytes)
        before = CollectionItem.objects.count()
        upload = SimpleUploadedFile("c.csv", csv_bytes + "Lightning Bolt,M10,Instant,R,NM,EN,0,4,1.50\n".encode())
        resp = self.client.post(reverse("ledger:import_csv"), {"file": upload})
        self.assertRedirects(resp, reverse("ledger:collection"))
        self.assertEqual(CollectionItem.objects.count(), before + 1)

    def test_import_rejects_bad_rows(self):
        upload = SimpleUploadedFile("c.csv", b"name,set,condition\nBolt,M10,XX\n")
        resp = self.client.post(reverse("ledger:import_csv"), {"file": upload})
        self.assertContains(resp, "invalid condition")
        self.assertFalse(CollectionItem.objects.filter(card__name="Bolt").exists())


class CollectionIsolationTest(LedgerTestCase):
    """seed_demo gave the demo collection to self.user; self.other is a friend with nothing yet."""

    def as_other(self):
        self.client.force_login(self.other)

    def test_friend_sees_empty_collection(self):
        self.as_other()
        resp = self.client.get(reverse("ledger:collection"))
        self.assertFalse(resp.context["stats"]["total"])
        self.assertEqual(resp.context["stats"]["unique"], 0)
        self.assertEqual(resp.context["rows"], [])
        self.assertEqual(list(resp.context["sets"]), [])
        self.assertEqual(resp.context["pending_changes"], 0)
        self.assertEqual(resp.context["orders_to_ship"], 0)
        self.assertNotContains(resp, "Ragavan")

    def test_item_of_other_user_is_404(self):
        item = CollectionItem.objects.filter(owner=self.user).first()
        self.as_other()
        self.assertEqual(self.client.get(reverse("ledger:item_edit", args=[item.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("ledger:item_delete", args=[item.pk])).status_code, 404)
        self.assertTrue(CollectionItem.objects.filter(pk=item.pk).exists())

    def test_csv_import_creates_own_item_for_same_printing(self):
        mine = CollectionItem.objects.get(owner=self.user, card__name="Thoughtseize")
        self.as_other()
        upload = SimpleUploadedFile("c.csv", b"name,set,condition,language,quantity,price\nThoughtseize,2XM,NM,EN,7,9.00\n")
        resp = self.client.post(reverse("ledger:import_csv"), {"file": upload})
        self.assertRedirects(resp, reverse("ledger:collection"))
        theirs = CollectionItem.objects.get(owner=self.other)
        self.assertEqual((theirs.card_id, theirs.quantity), (mine.card_id, 7))
        mine.refresh_from_db()
        self.assertEqual(mine.quantity, 3)
        self.assertTrue(SyncLog.objects.filter(owner=self.other, message__startswith="CSV import").exists())

    def test_add_cards_form_creates_own_item(self):
        self.as_other()
        self.client.post(reverse("ledger:item_create"), {
            "name": "Thoughtseize", "set_code": "2XM", "color": "B",
            "condition": "NM", "language": "EN", "quantity": 1, "price": "12.00",
        })
        self.assertEqual(CollectionItem.objects.get(owner=self.other).card.name, "Thoughtseize")
        self.assertEqual(CollectionItem.objects.get(owner=self.user, card__name="Thoughtseize").quantity, 3)

    def test_export_contains_only_own_rows(self):
        self.as_other()
        self.client.post(reverse("ledger:item_create"), {
            "name": "Lightning Bolt", "set_code": "M10", "color": "R",
            "condition": "NM", "language": "EN", "quantity": 1, "price": "1.00",
        })
        csv_text = self.client.get(reverse("ledger:export_csv")).content.decode()
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
        resp = self.client.post(reverse("ledger:listing_edit", args=[listing.item.pk]), {
            "quantity": 2, "price": "0.95", "market_price": "0.70", "status": "published",
        })
        self.assertRedirects(resp, reverse("ledger:sale"))
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
        messages = [str(m) for m in resp.wsgi_request._messages]
        self.assertTrue(any("not found in the CardTrader catalog" in m for m in messages))

    def test_api_error_is_shown(self):
        with mock.patch.object(services, "make_client", side_effect=CardTraderError("no token")):
            resp = self.client.post(reverse("ledger:publish"), follow=True)
        self.assertContains(resp, "Publish failed: no token")

    def test_delete_listing_removes_remote_product(self):
        self.publish()
        listing = Listing.objects.get(item__card__name="Counterspell")
        pid = listing.ct_product_id
        self.client.post(reverse("ledger:listing_delete", args=[listing.item.pk]))
        self.assertNotIn(pid, self.ct.products)
        self.assertFalse(Listing.objects.filter(pk=listing.pk).exists())


class MarketTest(LedgerTestCase):
    def test_match_market_prices_uses_same_condition_and_ignores_own_offers(self):
        self.client.post(reverse("ledger:match_prices"))
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
