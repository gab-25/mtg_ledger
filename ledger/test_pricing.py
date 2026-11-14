from decimal import Decimal
from unittest import mock

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
