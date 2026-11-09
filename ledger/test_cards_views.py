from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from .models import Blueprint, Card, CardmarketPrice, CollectionItem, Expansion, Listing
from .tests import LedgerTestCase


def catalog():
    """A small local catalog: CardTrader printings with Cardmarket prices."""
    m21 = Expansion.objects.create(ct_id=500, code="m21", name="Core Set 2021")
    dmr = Expansion.objects.create(ct_id=501, code="dmr", name="Dominaria Remastered")
    CardmarketPrice.objects.create(
        id_product=466234, trend=Decimal("12.75"), low=Decimal("10.00"), avg=Decimal("12.50"),
        trend_foil=Decimal("14.34"), prices_date=timezone.now(),
    )
    CardmarketPrice.objects.create(id_product=466300, trend=Decimal("0.50"))
    ugin = Blueprint.objects.create(
        ct_id=9001, expansion=m21, name="Ugin, the Spirit Dragon", collector_number="1",
        image_url="https://img/ugin.jpg", cm_price_id=466234,
    )
    Blueprint.objects.create(ct_id=9007, expansion=m21, name="Basri Ket", collector_number="7", cm_price_id=466300)
    Blueprint.objects.create(ct_id=9045, expansion=dmr, name="Counterspell", collector_number="45")
    return ugin


class ListingSuggestionTest(LedgerTestCase):
    def setUp(self):
        super().setUp()
        catalog()
        self.item = CollectionItem.objects.select_related("card").first()
        self.item.card.ct_blueprint_id = 9001
        self.item.card.save()
        self.url = reverse("ledger:api_listing", args=[self.item.pk])

    def suggestion(self):
        return self.client.get(self.url).json()["cm_suggestion"]

    def test_shows_trend_for_non_foil_item(self):
        self.item.foil = False
        self.item.save()
        self.assertEqual(self.suggestion(), "12.75")

    def test_shows_foil_trend_for_foil_item(self):
        self.item.foil = True
        self.item.save()
        self.assertEqual(self.suggestion(), "14.34")

    def test_foil_item_without_foil_trend_gets_no_suggestion(self):
        CardmarketPrice.objects.filter(pk=466234).update(trend_foil=None)
        self.item.foil = True
        self.item.save()
        self.assertIsNone(self.suggestion())

    def test_unknown_printing_shows_dash_and_no_button(self):
        self.item.card.ct_blueprint_id = None
        self.item.card.set_code = "PLST"
        self.item.card.save()
        self.assertIsNone(self.suggestion())

    def test_suggestion_does_not_change_saved_price(self):
        Listing.objects.filter(item=self.item).delete()
        self.suggestion()
        r = self.client.put(self.url, {"quantity": 1, "price": "3.00", "market_price": "", "status": "to_publish"},
                            content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Listing.objects.get(item=self.item).price, Decimal("3.00"))


class CardsPageTest(LedgerTestCase):
    def setUp(self):
        super().setUp()
        catalog()
        self.url = reverse("ledger:cards")

    def own(self, quantity, **card):
        item = CollectionItem.objects.select_related("card").first()
        item.quantity = quantity
        item.save()
        for k, v in card.items():
            setattr(item.card, k, v)
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
        self.assertContains(r, "M21 #1")
        for value in ("12.75", "10.00", "12.50", "14.34"):
            self.assertContains(r, value)
        self.assertContains(r, "https://img/ugin.jpg")
        self.assertNotContains(r, "Basri")

    def test_search_box_matches_set_code(self):
        r = self.client.get(self.url, {"q": "M21"})
        self.assertContains(r, "Ugin, the Spirit Dragon")
        self.assertContains(r, "Basri Ket")
        self.assertNotContains(r, "Counterspell")

    def test_set_filter(self):
        r = self.client.get(self.url, {"set": "DMR"})
        self.assertContains(r, "Counterspell")
        self.assertNotContains(r, "Ugin")

    def test_set_and_number_filter_ignores_leading_zeros(self):
        r = self.client.get(self.url, {"set": "m21", "number": "007"})
        self.assertContains(r, "Basri Ket")
        self.assertNotContains(r, "Ugin")

    def test_printing_without_price_shows_dashes(self):
        r = self.client.get(self.url, {"q": "counterspell"})
        self.assertContains(r, "Counterspell")
        self.assertNotContains(r, "€")

    def test_owned_by_blueprint_link(self):
        self.own(3, ct_blueprint_id=9001)
        r = self.client.get(self.url, {"owned": "1"})
        self.assertContains(r, "Ugin, the Spirit Dragon")
        self.assertNotContains(r, "Basri Ket")
        self.assertContains(r, '<td class="num">3</td>', html=True)

    def test_owned_by_set_and_number(self):
        self.own(2, ct_blueprint_id=None, set_code="DMR", collector_number="045")
        r = self.client.get(self.url, {"owned": "1"})
        self.assertContains(r, "Counterspell")
        self.assertContains(r, '<td class="num">2</td>', html=True)

    def test_owned_only_counts_mine(self):
        self.own(3, ct_blueprint_id=9001)
        self.client.force_login(self.other)
        r = self.client.get(self.url, {"owned": "1"})
        self.assertNotContains(r, "Ugin, the Spirit Dragon")
        r = self.client.get(self.url, {"q": "Ugin"})
        self.assertContains(r, "Ugin, the Spirit Dragon")
        self.assertNotContains(r, '<td class="num">3</td>', html=True)

    def test_no_criteria_lists_nothing(self):
        r = self.client.get(self.url, {"q": "  "})
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "Ugin")

    def test_no_results_message(self):
        r = self.client.get(self.url, {"q": "zzzz"})
        self.assertContains(r, "No cards match")

    def test_pager_keeps_filters(self):
        tokens = Expansion.objects.create(ct_id=502, code="tm21", name="M21 Tokens")
        Blueprint.objects.bulk_create(
            Blueprint(ct_id=20000 + i, expansion=tokens, name=f"Token {i}") for i in range(60)
        )
        r = self.client.get(self.url, {"set": "tm21"})
        self.assertContains(r, "set=tm21")

    def test_nav_link_present(self):
        r = self.client.get(reverse("ledger:collection"))
        self.assertContains(r, f'href="{self.url}"')
        self.assertContains(r, ">Cards</a>")
