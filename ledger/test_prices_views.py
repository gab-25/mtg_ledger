from decimal import Decimal

from django.urls import reverse

from .models import CardmarketProduct, CollectionItem, Listing
from .tests import LedgerTestCase


class ListingSuggestionTest(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.product = CardmarketProduct.objects.create(
            id_product=466234, name="Ugin, the Spirit Dragon", set_code="m21", collector_number="1",
            trend=Decimal("12.75"), trend_foil=Decimal("14.34"),
        )
        self.item = CollectionItem.objects.select_related("card").first()
        self.item.card.cm_product = self.product
        self.item.card.save()
        self.url = reverse("ledger:listing_edit", args=[self.item.pk])

    def test_shows_trend_for_non_foil_item(self):
        self.item.foil = False
        self.item.save()
        r = self.client.get(self.url)
        self.assertContains(r, "Cardmarket trend")
        self.assertContains(r, "12.75")
        self.assertContains(r, 'data-suggested-price="12.75"')

    def test_shows_foil_trend_for_foil_item(self):
        self.item.foil = True
        self.item.save()
        r = self.client.get(self.url)
        self.assertContains(r, 'data-suggested-price="14.34"')
        self.assertNotContains(r, 'data-suggested-price="12.75"')

    def test_unlinked_card_shows_dash_and_no_button(self):
        self.item.card.cm_product = None
        self.item.card.save()
        r = self.client.get(self.url)
        self.assertContains(r, "Cardmarket trend")
        self.assertNotContains(r, "data-suggested-price")

    def test_suggestion_does_not_change_saved_price(self):
        Listing.objects.filter(item=self.item).delete()
        r = self.client.post(self.url, {"quantity": 1, "price": "3.00", "market_price": "", "status": "to_publish"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Listing.objects.get(item=self.item).price, Decimal("3.00"))
