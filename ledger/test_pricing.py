from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from . import catalog, pricing
from .models import Expansion, MarketPrice
from .tests import LedgerTestCase

MARKET = ("GET", "/marketplace/products")


class PricingTest(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.api = self.ct.client()
        catalog.sync_expansions(self.api)
        self.neo = Expansion.objects.get(code="neo")

    def test_fill_expansion_one_call_counts_every_offer(self):
        """The shared price is the cheapest real offer, whoever sells it (the token owner included)."""
        pricing.fill_expansion(self.api, self.neo)
        self.assertEqual(self.ct.calls.count(MARKET), 1)
        nm = MarketPrice.objects.get(blueprint_ct_id=1100, language="IT", foil=False, condition="NM")
        self.assertEqual(nm.price, Decimal("1.00"))
        self.assertFalse(MarketPrice.objects.filter(price=Decimal("5.00")).exists())

    def test_price_for_prefers_same_condition(self):
        pricing.fill_expansion(self.api, self.neo)
        self.assertEqual(pricing.price_for(self.api, 1100, "IT", False, "NM"), Decimal("1.00"))
        self.assertEqual(pricing.price_for(self.api, 1100, "IT", False, "EX"), Decimal("2.00"))
        self.assertEqual(pricing.price_for(self.api, 1100, "IT", False, "GD"), Decimal("1.00"))  # none: cheapest
        self.assertEqual(self.ct.calls.count(MARKET), 1)  # served from the cache

    def test_price_for_stale_refetches(self):
        pricing.fill_expansion(self.api, self.neo)
        MarketPrice.objects.update(fetched_at=timezone.now() - timedelta(hours=7))
        price = pricing.price_for(self.api, 1100, "IT", False, "NM")
        self.assertEqual(self.ct.calls.count(MARKET), 2)
        self.assertEqual(price, Decimal("1.00"))  # per-blueprint offers: the 1.00 NM one counts too

    def test_price_for_api_error_returns_none(self):
        self.ct.fail_marketplace = True
        self.assertIsNone(pricing.price_for(self.api, 1100, "IT", False, "NM"))
