from decimal import Decimal

from django.test import TestCase

from . import cardmarket
from .models import Card, CardmarketProduct


class SuggestedPriceTest(TestCase):
    def setUp(self):
        self.product = CardmarketProduct.objects.create(
            id_product=466234, name="Ugin, the Spirit Dragon", set_code="m21", collector_number="1",
            trend=Decimal("12.75"), trend_foil=Decimal("14.34"),
        )
        self.card = Card.objects.create(
            name="Ugin, the Spirit Dragon", set_code="M21", collector_number="1", cm_product=self.product
        )

    def test_suggested_price_non_foil_uses_trend(self):
        self.assertEqual(cardmarket.suggested_price(self.card, foil=False), Decimal("12.75"))

    def test_suggested_price_foil_uses_trend_foil(self):
        self.assertEqual(cardmarket.suggested_price(self.card, foil=True), Decimal("14.34"))

    def test_suggested_price_foil_without_foil_trend(self):
        self.product.trend_foil = None
        self.product.save()
        self.assertIsNone(cardmarket.suggested_price(self.card, foil=True))

    def test_suggested_price_unlinked_card(self):
        card = Card.objects.create(name="X", set_code="PLST", collector_number="9")
        self.assertIsNone(cardmarket.suggested_price(card, foil=False))
