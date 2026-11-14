from decimal import Decimal
from unittest import mock

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from . import pricing
from .models import Card, CollectionItem, SyncLog
from .tests import LedgerTestCase

MARKET = ("GET", "/marketplace/products")


def offer(cents, cond=None):
    props = {"condition": cond} if cond else {}
    return {"price": {"cents": cents, "currency": "EUR"}, "user": {"id": 2}, "properties_hash": props}


class PricingTest(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.api = self.ct.client()

    def market_params(self):
        return [p for path, p in self.ct.call_params if path == "/marketplace/products"]

    def test_refresh_stores_cheapest_per_condition(self):
        self.ct.offers[1100] = [offer(300, "Near Mint"), offer(200, "Near Mint"), offer(150, "Slightly Played")]
        self.assertEqual(pricing.refresh(self.api, 1100, "IT", False), 2)
        self.assertEqual(pricing.price(1100, "IT", False, "NM"), Decimal("2.00"))
        self.assertEqual(pricing.price(1100, "IT", False, "EX"), Decimal("1.50"))
        self.assertIsNone(pricing.price(1100, "IT", False, "GD"))
        self.assertIsNone(pricing.price(1100, "EN", False, "NM"))
        params = self.market_params()[0]
        self.assertEqual((params["language"], params["foil"]), ("it", "false"))

    def test_refresh_replaces_old_rows(self):
        self.ct.offers[1100] = [offer(300, "Near Mint")]
        pricing.refresh(self.api, 1100, "IT", False)
        self.ct.offers[1100] = [offer(150, "Slightly Played")]
        pricing.refresh(self.api, 1100, "IT", False)
        self.assertIsNone(pricing.price(1100, "IT", False, "NM"))
        self.assertEqual(pricing.price(1100, "IT", False, "EX"), Decimal("1.50"))

    def test_refresh_product_stores_nm(self):
        self.ct.offers[1390] = [offer(400), offer(350)]
        self.assertEqual(pricing.refresh(self.api, 1390, "EN", False, is_product=True), 1)
        self.assertEqual(pricing.price(1390, "EN", False, "NM"), Decimal("3.50"))
        params = self.market_params()[0]
        self.assertNotIn("language", params)
        self.assertNotIn("foil", params)

    def item(self, ct_id=1100, language="IT", foil=False, condition="NM"):
        card, _ = Card.objects.get_or_create(
            name="Fable", set_code="ZZZ", collector_number=str(ct_id or "x"), defaults={"ct_blueprint_id": ct_id},
        )
        return CollectionItem.objects.create(
            owner=self.user, card=card, language=language, foil=foil, condition=condition, quantity=1,
        )

    def test_ensure_skips_cached_combination(self):
        self.ct.offers[1100] = [offer(300, "Near Mint")]
        item = self.item()
        pricing.ensure(self.api, item)
        pricing.ensure(self.api, item)
        self.assertEqual(self.ct.calls.count(MARKET), 1)
        self.assertEqual(pricing.price(1100, "IT", False, "NM"), Decimal("3.00"))

    def test_ensure_api_error_logs_warning(self):
        self.ct.fail_marketplace = True
        pricing.ensure(self.api, self.item())
        log = SyncLog.objects.filter(level=SyncLog.Level.WARNING).latest("id")
        self.assertTrue(log.message.startswith("Market price unavailable"))

    def test_ensure_without_blueprint_does_nothing(self):
        item = self.item(ct_id=None)
        pricing.ensure(self.api, item)
        self.assertEqual(self.ct.calls.count(MARKET), 0)

    def test_with_price_matches_exact_combination(self):
        self.ct.offers[1100] = [offer(300, "Near Mint"), offer(150, "Slightly Played")]
        pricing.refresh(self.api, 1100, "IT", False)
        nm, gd = self.item(), self.item(condition="GD")
        prices = dict(pricing.with_price(CollectionItem.objects.filter(pk__in=[nm.pk, gd.pk]))
                      .values_list("pk", "market_price"))
        self.assertEqual(prices, {nm.pk: Decimal("3.00"), gd.pk: None})


class RefreshOwnedTest(PricingTest):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(pricing, "PAUSE", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        CollectionItem.objects.all().delete()

    def test_refreshes_every_users_combinations(self):
        mine = self.item(1100)
        CollectionItem.objects.create(owner=self.other, card=mine.card, language="EN", foil=True,
                                               condition="NM", quantity=1)
        CollectionItem.objects.create(owner=self.other, card=mine.card, language="EN", foil=True,
                                      condition="EX", quantity=1)  # same combination: one call
        self.item(None)
        self.assertEqual(pricing.owned_combinations(), [(1100, "EN", True, False), (1100, "IT", False, False)])
        self.assertEqual(pricing.refresh_owned(self.api), (2, 0))
        self.assertEqual(self.ct.calls.count(MARKET), 2)

    def test_refresh_owned_counts_failures(self):
        self.item(1100)
        self.ct.fail_marketplace = True
        self.assertEqual(pricing.refresh_owned(self.api), (0, 1))


class PricingRulesTest(PricingTest):
    def test_one_broken_combination_does_not_stop_the_others(self):
        CollectionItem.objects.all().delete()
        self.item(1100)
        self.item(1101)
        self.ct.offers[1100] = "not a list of offers"  # an unexpected payload
        self.ct.offers[1101] = [offer(300, "Near Mint")]
        with mock.patch.object(pricing, "PAUSE", 0):
            self.assertEqual(pricing.refresh_owned(self.api), (1, 1))
        self.assertEqual(pricing.price(1101, "IT", False, "NM"), Decimal("3.00"))

    def test_product_with_an_old_foil_row_prices_as_nm_non_foil(self):
        card = Card.objects.create(name="Box", set_code="ZZZ", collector_number="CT1390", ct_blueprint_id=1390,
                                   is_product=True)
        item = CollectionItem.objects.create(owner=self.user, card=card, language="EN", foil=True, condition="EX",
                                             quantity=1)
        self.ct.offers[1390] = [offer(4000)]
        pricing.ensure(self.api, item)
        self.assertEqual(pricing.item_price(item), Decimal("40.00"))
        annotated = pricing.with_price(CollectionItem.objects.filter(pk=item.pk)).get()
        self.assertEqual(annotated.market_price, Decimal("40.00"))

    def test_old_lookup_helpers_are_gone(self):
        for name in ("price_for", "fill_expansion", "PRICE_TTL"):
            self.assertFalse(hasattr(pricing, name), name)


class ClearOldPricesMigrationTest(TransactionTestCase):
    """0024 empties the price cache: its rows were written by the old rules (any condition, expansion calls)."""

    BEFORE = [("ledger", "0023_blueprint_image_large_url")]
    AFTER = [("ledger", "0024_remove_cardmarket")]

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_market_prices_are_cleared(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.BEFORE)
        apps = executor.loader.project_state(self.BEFORE).apps
        apps.get_model("ledger", "MarketPrice").objects.create(
            blueprint_ct_id=1, language="EN", foil=False, condition="NM", price=Decimal("1.00"), fetched_at=timezone.now(),
        )
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(self.AFTER)
        apps = executor.loader.project_state(self.AFTER).apps
        self.assertFalse(apps.get_model("ledger", "MarketPrice").objects.exists())
