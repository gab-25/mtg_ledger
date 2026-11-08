import copy
import gzip
import json
from decimal import Decimal
from io import StringIO
from unittest import mock

import httpx
from django.core.management import call_command
from django.test import TestCase

from . import cardmarket
from .models import Card, CardmarketProduct, SyncLog

SCRYFALL_DOWNLOAD = "https://data.scryfall.io/default-cards/default-cards-20261108100757.jsonl.gz"

PRODUCTS = {
    "version": 1, "createdAt": "2026-11-08T13:01:01+0100",
    "products": [
        {"idProduct": 466234, "name": "Ugin, the Spirit Dragon", "idCategory": 1, "categoryName": "Magic Single",
         "idExpansion": 2864, "idMetacard": 1, "dateAdded": "2020-06-01 00:00:00"},
        {"idProduct": 900, "name": "Counterspell", "idCategory": 1, "categoryName": "Magic Single",
         "idExpansion": 5000, "idMetacard": 2, "dateAdded": "2022-01-01 00:00:00"},
        {"idProduct": 1, "name": "Altar's Light", "idCategory": 1, "categoryName": "Magic Single",
         "idExpansion": 45, "idMetacard": 129, "dateAdded": "2007-01-01 00:00:00"},
    ],
}

GUIDE = {
    "version": 1, "createdAt": "2026-11-07T02:45:31+0100",
    "priceGuides": [
        {"idProduct": 466234, "idCategory": 1, "avg": 12.5, "low": 10.0, "trend": 12.75, "avg1": 12.0,
         "avg7": 12.6, "avg30": 13.0, "avg-foil": 14.0, "low-foil": None, "trend-foil": 14.34,
         "avg1-foil": 14.1, "avg7-foil": 14.2, "avg30-foil": 14.3},
        {"idProduct": 1, "idCategory": 1, "trend": 0.06},
        {"idProduct": 777777, "idCategory": 1, "trend": 99},
    ],
}

SCRYFALL_CARDS = [
    {"name": "Ugin, the Spirit Dragon", "cardmarket_id": 466234, "set": "m21", "collector_number": "1",
     "image_uris": {"normal": "https://img/ugin.jpg"}},
    {"name": "Counterspell", "cardmarket_id": 900, "set": "dmr", "collector_number": "45",
     "card_faces": [{"image_uris": {"normal": "https://img/cs.jpg"}}]},
    {"name": "No Cardmarket", "set": "xyz", "collector_number": "7"},
]


class FakeSources:
    """The Cardmarket files and Scryfall bulk data, served through httpx.MockTransport."""

    def __init__(self):
        self.products = copy.deepcopy(PRODUCTS)
        self.guide = copy.deepcopy(GUIDE)
        self.scryfall_cards = copy.deepcopy(SCRYFALL_CARDS)
        self.fail = set()  # source keys answering 500
        self.content_encoding = False  # serve Scryfall with Content-Encoding: gzip (httpx inflates it)
        self.raw = {}  # source key -> raw body served instead of JSON

    def _serve(self, key, payload):
        if key in self.fail:
            return httpx.Response(500, text="boom")
        if key in self.raw:
            return httpx.Response(200, content=self.raw[key])
        return httpx.Response(200, json=payload)

    def handler(self, request):
        url = str(request.url)
        if url == cardmarket.PRODUCTS_URL:
            return self._serve("products", self.products)
        if url == cardmarket.PRICE_GUIDE_URL:
            return self._serve("guide", self.guide)
        if url == cardmarket.SCRYFALL_BULK_URL:
            return httpx.Response(200, json={"data": [
                {"type": "oracle_cards", "jsonl_download_uri": "https://data.scryfall.io/oracle.jsonl.gz"},
                {"type": "default_cards", "jsonl_download_uri": SCRYFALL_DOWNLOAD, "compressed_size": 1},
            ]})
        if url == SCRYFALL_DOWNLOAD:
            if "scryfall" in self.fail:
                return httpx.Response(500, text="boom")
            body = self.raw.get("scryfall") or "\n".join(json.dumps(c) for c in self.scryfall_cards).encode()
            if self.content_encoding:
                return httpx.Response(200, content=gzip.compress(body), headers={"Content-Encoding": "gzip"})
            return httpx.Response(200, content=gzip.compress(body), headers={"Content-Type": "application/gzip"})
        return httpx.Response(404)

    def client(self):
        return cardmarket.make_client(transport=httpx.MockTransport(self.handler))


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


class SyncTest(TestCase):
    def setUp(self):
        self.sources = FakeSources()
        self.client_ = self.sources.client()
        self.addCleanup(self.client_.close)

    def test_sync_creates_products_with_prices_and_printing(self):
        result = cardmarket.sync(self.client_)
        ugin = CardmarketProduct.objects.get(pk=466234)
        self.assertEqual((ugin.name, ugin.id_expansion), ("Ugin, the Spirit Dragon", 2864))
        self.assertEqual((ugin.set_code, ugin.collector_number, ugin.image_url), ("m21", "1", "https://img/ugin.jpg"))
        self.assertEqual((ugin.trend, ugin.trend_foil, ugin.low_foil), (Decimal("12.75"), Decimal("14.34"), None))
        self.assertEqual((ugin.low, ugin.avg, ugin.avg30_foil), (Decimal("10.00"), Decimal("12.50"), Decimal("14.30")))
        self.assertEqual(ugin.prices_date.isoformat(), "2026-11-07T01:45:31+00:00")
        self.assertEqual(CardmarketProduct.objects.get(pk=900).image_url, "https://img/cs.jpg")
        self.assertEqual((result.products, result.priced), (3, 2))

    def test_scryfall_served_with_content_encoding(self):
        self.sources.content_encoding = True
        cardmarket.sync(self.client_)
        self.assertEqual(CardmarketProduct.objects.get(pk=466234).set_code, "m21")

    def test_unmatched_product_kept_name_only(self):
        cardmarket.sync(self.client_)
        p = CardmarketProduct.objects.get(pk=1)
        self.assertEqual((p.name, p.set_code, p.trend), ("Altar's Light", "", Decimal("0.06")))

    def test_product_without_guide_row_has_no_prices(self):
        cardmarket.sync(self.client_)
        cs = CardmarketProduct.objects.get(pk=900)
        self.assertEqual((cs.trend, cs.trend_foil), (None, None))

    def test_price_guide_rows_for_unknown_products_ignored(self):
        cardmarket.sync(self.client_)
        self.assertFalse(CardmarketProduct.objects.filter(pk=777777).exists())

    def test_sync_twice_no_duplicates_prices_overwritten(self):
        cardmarket.sync(self.client_)
        self.sources.guide["priceGuides"][0]["trend"] = 20.0
        cardmarket.sync(self.client_)
        self.assertEqual(CardmarketProduct.objects.count(), 3)
        self.assertEqual(CardmarketProduct.objects.get(pk=466234).trend, Decimal("20.00"))

    def test_links_cards_case_insensitive_and_leading_zeros(self):
        cs = Card.objects.create(name="Counterspell", set_code="DMR", collector_number="045")
        ugin = Card.objects.create(name="Ugin", set_code="M21", collector_number="1")
        other = Card.objects.create(name="Ugin", set_code="PLST", collector_number="1")
        legacy = Card.objects.create(name="Counterspell", set_code="DMR", collector_number="")
        result = cardmarket.sync(self.client_)
        for card, expected in ((cs, 900), (ugin, 466234), (other, None), (legacy, None)):
            card.refresh_from_db()
            self.assertEqual(card.cm_product_id, expected, card)
        self.assertEqual((result.linked, result.unlinked), (2, 2))

    def test_link_removed_when_printing_no_longer_matches(self):
        card = Card.objects.create(name="Ugin", set_code="M21", collector_number="1")
        cardmarket.sync(self.client_)
        self.sources.scryfall_cards[0]["collector_number"] = "2"
        cardmarket.sync(self.client_)
        card.refresh_from_db()
        self.assertIsNone(card.cm_product_id)

    def test_source_failure_changes_nothing_and_logs_warning(self):
        CardmarketProduct.objects.create(id_product=466234, name="Ugin", trend=Decimal("1.00"))
        self.sources.fail.add("guide")
        self.assertIsNone(cardmarket.run_sync(self.client_))
        self.assertEqual(CardmarketProduct.objects.get(pk=466234).trend, Decimal("1.00"))
        self.assertEqual(CardmarketProduct.objects.count(), 1)
        log = SyncLog.objects.first()
        self.assertEqual(log.level, SyncLog.Level.WARNING)
        self.assertIn("Cardmarket sync failed", log.message)

    def test_scryfall_failure_changes_nothing(self):
        self.sources.fail.add("scryfall")
        self.assertIsNone(cardmarket.run_sync(self.client_))
        self.assertEqual(CardmarketProduct.objects.count(), 0)

    def test_malformed_json_changes_nothing(self):
        self.sources.raw["products"] = b"not json"
        self.assertIsNone(cardmarket.run_sync(self.client_))
        self.assertEqual(CardmarketProduct.objects.count(), 0)

    def test_malformed_scryfall_changes_nothing(self):
        self.sources.raw["scryfall"] = b'{"cardmarket_id": 466234}\n{"cardmarket_id": 1, '
        self.assertIsNone(cardmarket.run_sync(self.client_))
        self.assertEqual(CardmarketProduct.objects.count(), 0)

    def test_duplicate_product_rows_do_not_break_sync(self):
        self.sources.products["products"].append(dict(self.sources.products["products"][0]))
        result = cardmarket.sync(self.client_)
        self.assertEqual(result.products, 3)
        self.assertEqual(CardmarketProduct.objects.count(), 3)

    def test_unexpected_failure_is_logged_and_raised(self):
        with mock.patch.object(CardmarketProduct.objects, "bulk_create", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                cardmarket.run_sync(self.client_)
        log = SyncLog.objects.first()
        self.assertEqual(log.level, SyncLog.Level.WARNING)
        self.assertEqual(log.message, "Cardmarket sync failed: RuntimeError: db down")

    def test_run_sync_logs_counts(self):
        Card.objects.create(name="Ugin", set_code="M21", collector_number="1")
        cardmarket.run_sync(self.client_)
        log = SyncLog.objects.first()
        self.assertEqual(log.level, SyncLog.Level.OK)
        self.assertEqual(
            log.message,
            "Cardmarket: 3 products, 2 priced (guide 2026-11-07), 1 cards linked, 0 not linked",
        )


class SyncCommandTest(TestCase):
    def test_command_only_enqueues(self):
        from . import tasks

        with mock.patch.object(tasks.sync_cardmarket, "delay", return_value=mock.Mock(id="abc")) as delay, \
                mock.patch.object(cardmarket, "run_sync") as run:
            out = StringIO()
            call_command("sync_cardmarket", stdout=out)
        delay.assert_called_once_with()
        run.assert_not_called()
        self.assertIn("Cardmarket sync queued (task abc)", out.getvalue())

    def test_task_runs_sync(self):
        from . import tasks

        with mock.patch.object(cardmarket, "run_sync") as run:
            tasks.sync_cardmarket.apply()
        run.assert_called_once_with()

    def test_no_beat_schedule(self):
        from config.celery import app

        self.assertFalse(app.conf.beat_schedule)
