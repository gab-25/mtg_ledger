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


class CardsPageTest(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.ugin = CardmarketProduct.objects.create(
            id_product=466234, name="Ugin, the Spirit Dragon", set_code="m21", collector_number="1",
            image_url="https://img/ugin.jpg", trend=Decimal("12.75"), low=Decimal("10.00"), avg=Decimal("12.50"),
            trend_foil=Decimal("14.34"),
        )
        CardmarketProduct.objects.create(
            id_product=466300, name="Basri Ket", set_code="m21", collector_number="7", trend=Decimal("0.50"),
        )
        CardmarketProduct.objects.create(
            id_product=900, name="Counterspell", set_code="dmr", collector_number="45", trend=Decimal("1.20"),
        )
        CardmarketProduct.objects.create(id_product=1, name="Altar's Light", trend=Decimal("0.06"))
        self.url = reverse("ledger:cards")

    def own_ugin(self, quantity):
        item = CollectionItem.objects.select_related("card").first()
        item.quantity = quantity
        item.save()
        item.card.cm_product = self.ugin
        item.card.save()

    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_page_is_called_cards(self):
        r = self.client.get(self.url)
        self.assertContains(r, "<h1>Cards</h1>", html=True)

    def test_search_name_case_insensitive_substring(self):
        r = self.client.get(self.url, {"q": "spirit drag"})
        self.assertContains(r, "Ugin, the Spirit Dragon")
        self.assertContains(r, "M21")
        for value in ("12.75", "10.00", "12.50", "14.34"):
            self.assertContains(r, value)
        self.assertContains(r, "https://img/ugin.jpg")
        self.assertNotContains(r, "Altar")

    def test_search_box_matches_set_code(self):
        r = self.client.get(self.url, {"q": "M21"})
        self.assertContains(r, "Ugin, the Spirit Dragon")
        self.assertContains(r, "Basri Ket")
        self.assertNotContains(r, "Counterspell")

    def test_set_filter(self):
        r = self.client.get(self.url, {"set": "dmr"})
        self.assertContains(r, "Counterspell")
        self.assertNotContains(r, "Ugin")

    def test_set_and_number_filter_ignores_leading_zeros(self):
        r = self.client.get(self.url, {"set": "M21", "number": "007"})
        self.assertContains(r, "Basri Ket")
        self.assertNotContains(r, "Ugin")

    def test_owned_filter_and_quantity(self):
        self.own_ugin(3)
        r = self.client.get(self.url, {"owned": "1"})
        self.assertContains(r, "Ugin, the Spirit Dragon")
        self.assertNotContains(r, "Basri Ket")
        self.assertContains(r, '<td class="num">3</td>', html=True)

    def test_no_criteria_lists_nothing(self):
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
        self.assertContains(r, "No cards match")

    def test_pager_keeps_filters(self):
        for i in range(60):
            CardmarketProduct.objects.create(id_product=10000 + i, name=f"Token {i}", set_code="tm21")
        r = self.client.get(self.url, {"set": "tm21"})
        self.assertContains(r, "set=tm21")

    def test_nav_link_present(self):
        r = self.client.get(reverse("ledger:collection"))
        self.assertContains(r, f'href="{self.url}"')
        self.assertContains(r, ">Cards</a>")
