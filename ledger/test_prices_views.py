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


class PricesPageTest(LedgerTestCase):
    def setUp(self):
        super().setUp()
        CardmarketProduct.objects.create(
            id_product=466234, name="Ugin, the Spirit Dragon", set_code="m21", collector_number="1",
            image_url="https://img/ugin.jpg", trend=Decimal("12.75"), low=Decimal("10.00"), avg=Decimal("12.50"),
            trend_foil=Decimal("14.34"),
        )
        CardmarketProduct.objects.create(id_product=1, name="Altar's Light", trend=Decimal("0.06"))
        self.url = reverse("ledger:prices")

    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_search_case_insensitive_substring(self):
        r = self.client.get(self.url, {"q": "spirit drag"})
        self.assertContains(r, "Ugin, the Spirit Dragon")
        self.assertContains(r, "M21")
        for value in ("12.75", "10.00", "12.50", "14.34"):
            self.assertContains(r, value)
        self.assertContains(r, "https://img/ugin.jpg")
        self.assertNotContains(r, "Altar")

    def test_empty_query_lists_nothing(self):
        r = self.client.get(self.url, {"q": "  "})
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "Ugin")
        self.assertNotContains(r, "Altar")

    def test_unmatched_product_shown_without_set(self):
        r = self.client.get(self.url, {"q": "altar"})
        self.assertContains(r, "Altar&#x27;s Light")
        self.assertContains(r, "0.06")

    def test_no_results_message(self):
        r = self.client.get(self.url, {"q": "zzzz"})
        self.assertContains(r, "No Cardmarket cards match")

    def test_nav_link_present(self):
        r = self.client.get(reverse("ledger:collection"))
        self.assertContains(r, f'href="{self.url}"')
