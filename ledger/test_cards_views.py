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
        self.url = reverse("ledger:api_cards")

    def own(self, quantity, **card):
        item = CollectionItem.objects.select_related("card").first()
        item.quantity = quantity
        item.save()
        for k, v in card.items():
            setattr(item.card, k, v)
        item.card.save()

    def search(self, **params):
        resp = self.client.get(self.url, params)
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def names(self, **params):
        return [p["name"] for p in self.search(**params)["results"]]

    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_search_name_case_insensitive_substring(self):
        data = self.search(q="spirit drag")
        self.assertTrue(data["searched"])
        [ugin] = data["results"]
        self.assertEqual((ugin["name"], ugin["expansion_code"], ugin["collector_number"]),
                         ("Ugin, the Spirit Dragon", "m21", "1"))
        self.assertEqual(ugin["expansion_name"], "Core Set 2021")
        self.assertEqual(ugin["prices"], {"trend": "12.75", "low": "10.00", "avg": "12.50",
                                          "trend_foil": "14.34", "low_foil": None, "avg_foil": None})
        self.assertEqual(ugin["image_url"], "https://img/ugin.jpg")
        self.assertIsNone(ugin["owned_qty"])
        self.assertIsNotNone(data["prices_date"])

    def test_search_box_matches_set_code(self):
        self.assertEqual(self.names(q="M21"), ["Basri Ket", "Ugin, the Spirit Dragon"])

    def test_set_filter(self):
        self.assertEqual(self.names(set="DMR"), ["Counterspell"])

    def test_set_and_number_filter_ignores_leading_zeros(self):
        data = self.search(set="m21", number="007")
        self.assertEqual([p["name"] for p in data["results"]], ["Basri Ket"])
        self.assertEqual(data["filters"], {"q": "", "set": "m21", "number": "7", "owned": False})

    def test_printing_without_price_shows_dashes(self):
        [card] = self.search(q="counterspell")["results"]
        self.assertIsNone(card["prices"])

    def test_owned_by_blueprint_link(self):
        self.own(3, ct_blueprint_id=9001)
        data = self.search(owned="1")
        self.assertEqual([(p["name"], p["owned_qty"]) for p in data["results"]], [("Ugin, the Spirit Dragon", 3)])
        self.assertTrue(data["filters"]["owned"])

    def test_owned_by_set_and_number(self):
        self.own(2, ct_blueprint_id=None, set_code="DMR", collector_number="045")
        self.assertEqual([(p["name"], p["owned_qty"]) for p in self.search(owned="1")["results"]],
                         [("Counterspell", 2)])

    def test_owned_only_counts_mine(self):
        self.own(3, ct_blueprint_id=9001)
        self.client.force_login(self.other)
        self.assertEqual(self.names(owned="1"), [])
        [ugin] = self.search(q="Ugin")["results"]
        self.assertIsNone(ugin["owned_qty"])

    def test_no_criteria_lists_nothing(self):
        data = self.search(q="  ")
        self.assertEqual(data, {"searched": False, "filters": {"q": "", "set": "", "number": "", "owned": False}})

    def test_no_results_message(self):
        data = self.search(q="zzzz")
        self.assertEqual((data["searched"], data["count"], data["results"]), (True, 0, []))

    def test_pager_keeps_filters(self):
        tokens = Expansion.objects.create(ct_id=502, code="tm21", name="M21 Tokens")
        Blueprint.objects.bulk_create(
            Blueprint(ct_id=20000 + i, expansion=tokens, name=f"Token {i}") for i in range(60)
        )
        data = self.search(set="tm21", page="2")
        self.assertEqual((data["count"], data["page"], data["num_pages"], len(data["results"])), (60, 2, 2, 10))
