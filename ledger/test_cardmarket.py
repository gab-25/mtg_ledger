import copy
from decimal import Decimal
from io import StringIO
from unittest import mock

import httpx
from django.core.management import call_command
from django.test import TestCase

from . import cardmarket, services
from .models import Blueprint, Card, CardmarketPrice, Category, Expansion, SyncLog
from .tests import FakeCardTrader

GUIDE = {
    "version": 1, "createdAt": "2026-11-07T02:45:31+0100",
    "priceGuides": [
        {"idProduct": 555001, "idCategory": 1, "avg": 40.5, "low": 35.0, "trend": 42.0, "avg1": 41.0,
         "avg7": 41.5, "avg30": 43.0, "avg-foil": 60.0, "low-foil": None, "trend-foil": 61.25,
         "avg1-foil": 59.0, "avg7-foil": 60.5, "avg30-foil": 62.0},
        {"idProduct": 555201, "idCategory": 1, "trend": 1.2},
        {"idProduct": 999999, "idCategory": 1, "trend": 99},
    ],
}

# CardTrader blueprint id -> Cardmarket ids (FakeCardTrader's catalog has none of its own)
CM_IDS = {1001: [555001], 1201: [555201, 555202], 1002: []}


class FakeGuide:
    """Cardmarket's price guide file, served through httpx.MockTransport."""

    def __init__(self):
        self.guide = copy.deepcopy(GUIDE)
        self.fail = False
        self.raw = None

    def handler(self, request):
        if str(request.url) != cardmarket.PRICE_GUIDE_URL:
            return httpx.Response(404)
        if self.fail:
            return httpx.Response(500, text="boom")
        if self.raw is not None:
            return httpx.Response(200, content=self.raw)
        return httpx.Response(200, json=self.guide)

    def client(self):
        return cardmarket.make_client(transport=httpx.MockTransport(self.handler))


class CardmarketTestCase(TestCase):
    def setUp(self):
        self.ct = FakeCardTrader()
        for expansion in self.ct.blueprints.values():
            for bp in expansion:
                bp["card_market_ids"] = CM_IDS.get(bp["id"], [])
                bp["image_url"] = f"https://img/{bp['id']}.jpg"
        self.guide = FakeGuide()
        self.cm = self.guide.client()
        self.addCleanup(self.cm.close)
        for target, attr, value in (
            (services, "make_client", mock.Mock(side_effect=self.ct.client)),
            (cardmarket, "BLUEPRINT_PAUSE", 0),
        ):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def sync(self):
        with self.ct.client() as ct:
            return cardmarket.sync(self.cm, ct)


class SuggestedPriceTest(CardmarketTestCase):
    def setUp(self):
        super().setUp()
        self.sync()

    def test_non_foil_uses_trend_via_blueprint_link(self):
        card = Card.objects.create(name="Ragavan", set_code="MH2", collector_number="138", ct_blueprint_id=1001)
        self.assertEqual(cardmarket.suggested_price(card, foil=False), Decimal("42.00"))

    def test_foil_uses_trend_foil(self):
        card = Card.objects.create(name="Ragavan", set_code="MH2", collector_number="138", ct_blueprint_id=1001)
        self.assertEqual(cardmarket.suggested_price(card, foil=True), Decimal("61.25"))

    def test_foil_without_foil_trend(self):
        CardmarketPrice.objects.filter(pk=555201).update(trend_foil=None)
        card = Card.objects.create(name="Counterspell", set_code="DMR", collector_number="45", ct_blueprint_id=1201)
        self.assertIsNone(cardmarket.suggested_price(card, foil=True))

    def test_card_without_blueprint_found_by_set_and_number(self):
        card = Card.objects.create(name="Counterspell", set_code="DMR", collector_number="045")
        self.assertEqual(cardmarket.suggested_price(card, foil=False), Decimal("1.20"))

    def test_unknown_printing(self):
        card = Card.objects.create(name="X", set_code="PLST", collector_number="9")
        self.assertIsNone(cardmarket.suggested_price(card, foil=False))

    def test_blueprint_without_cardmarket_id(self):
        card = Card.objects.create(name="Solitude", set_code="MH2", collector_number="32", ct_blueprint_id=1002)
        self.assertIsNone(cardmarket.suggested_price(card, foil=False))


class SyncTest(CardmarketTestCase):
    def test_sync_loads_every_mtg_expansion_and_blueprint(self):
        result = self.sync()
        self.assertEqual(set(Expansion.objects.values_list("code", flat=True)), {"mh2", "neo", "dmr", "dmu"})
        self.assertEqual(Blueprint.objects.count(), 10)
        self.assertFalse(Expansion.objects.filter(blueprints_synced_at__isnull=True).exists())
        rag = Blueprint.objects.get(ct_id=1001)
        self.assertEqual((rag.name, rag.expansion.code, rag.collector_number), ("Ragavan, Nimble Pilferer", "mh2", "138"))
        self.assertEqual((rag.cm_price_id, rag.image_url), (555001, "https://img/1001.jpg"))
        self.assertEqual((result.expansions, result.blueprints, result.priced), (4, 10, 2))

    def test_sync_stores_categories_and_properties(self):
        self.sync()
        self.assertEqual(Category.objects.get(ct_id=40).name, "Playmats")
        box = Blueprint.objects.get(ct_id=1390)
        self.assertEqual((box.category.ct_id, box.editable_properties), (4, ["mtg_language"]))
        self.assertTrue(box.is_product)
        self.assertFalse(Blueprint.objects.get(ct_id=1001).is_product)

    def test_long_collector_number_stored(self):
        # CardTrader numbers for some promo/anniversary printings, e.g. "10-013T/012-019T"
        self.ct.blueprints[next(iter(self.ct.blueprints))][0]["fixed_properties"]["collector_number"] = "10-013T/012-019T"
        self.sync()
        self.assertTrue(Blueprint.objects.filter(collector_number="10-013T/012-019T").exists())
        card = Card.objects.create(name="Token", set_code="SLD", collector_number="10-013T/012-019T")
        self.assertEqual(Card.objects.get(pk=card.pk).collector_number, "10-013T/012-019T")

    def test_first_cardmarket_id_is_used(self):
        self.sync()
        self.assertEqual(Blueprint.objects.get(ct_id=1201).cm_price_id, 555201)

    def test_blueprint_without_cardmarket_id_has_no_price(self):
        self.sync()
        self.assertIsNone(Blueprint.objects.get(ct_id=1002).cm_price_id)

    def test_prices_stored(self):
        self.sync()
        p = CardmarketPrice.objects.get(pk=555001)
        self.assertEqual((p.trend, p.trend_foil, p.low_foil), (Decimal("42.00"), Decimal("61.25"), None))
        self.assertEqual((p.low, p.avg, p.avg30_foil), (Decimal("35.00"), Decimal("40.50"), Decimal("62.00")))
        self.assertEqual(p.prices_date.isoformat(), "2026-11-07T01:45:31+00:00")

    def test_sync_twice_no_duplicates_prices_overwritten(self):
        self.sync()
        self.guide.guide["priceGuides"][0]["trend"] = 50.0
        self.ct.blueprints[10][0]["name"] = "Ragavan (renamed)"
        self.sync()
        self.assertEqual(Blueprint.objects.count(), 10)
        self.assertEqual(CardmarketPrice.objects.get(pk=555001).trend, Decimal("50.00"))
        self.assertEqual(Blueprint.objects.get(ct_id=1001).name, "Ragavan (renamed)")

    def test_duplicate_guide_rows_do_not_break_sync(self):
        self.guide.guide["priceGuides"].append(dict(self.guide.guide["priceGuides"][0]))
        self.sync()
        self.assertEqual(CardmarketPrice.objects.filter(pk=555001).count(), 1)

    def test_failed_expansion_skipped_and_reported(self):
        self.ct.fail_export = {11}
        result = self.sync()
        self.assertEqual(result.failed, ["neo"])
        self.assertFalse(Blueprint.objects.filter(expansion__code="neo").exists())
        self.assertTrue(Blueprint.objects.filter(ct_id=1001).exists())
        self.assertIsNone(Expansion.objects.get(code="neo").blueprints_synced_at)

    def test_guide_failure_changes_nothing_and_logs_warning(self):
        CardmarketPrice.objects.create(id_product=555001, trend=Decimal("1.00"))
        self.guide.fail = True
        self.assertIsNone(cardmarket.run_sync(self.cm))
        self.assertEqual(CardmarketPrice.objects.get(pk=555001).trend, Decimal("1.00"))
        self.assertFalse(Blueprint.objects.exists())
        log = SyncLog.objects.first()
        self.assertEqual(log.level, SyncLog.Level.WARNING)
        self.assertIn("Cardmarket sync failed", log.message)

    def test_malformed_guide_changes_nothing(self):
        self.guide.raw = b"not json"
        self.assertIsNone(cardmarket.run_sync(self.cm))
        self.assertFalse(Blueprint.objects.exists())

    def test_cardtrader_expansions_failure_changes_nothing(self):
        with mock.patch.object(FakeCardTrader, "handler", lambda s, r: httpx.Response(500, json={})):
            self.assertIsNone(cardmarket.run_sync(self.cm))
        self.assertFalse(CardmarketPrice.objects.exists())
        self.assertIn("Cardmarket sync failed", SyncLog.objects.first().message)

    def test_missing_cardtrader_token_logs_warning(self):
        services.make_client.side_effect = services.CardTraderError("CardTrader API token is not configured.")
        self.assertIsNone(cardmarket.run_sync(self.cm))
        self.assertIn("token is not configured", SyncLog.objects.first().message)

    def test_unexpected_failure_is_logged_and_raised(self):
        with mock.patch.object(CardmarketPrice.objects, "bulk_create", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                cardmarket.run_sync(self.cm)
        log = SyncLog.objects.first()
        self.assertEqual(log.level, SyncLog.Level.WARNING)
        self.assertEqual(log.message, "Cardmarket sync failed: RuntimeError: db down")

    def test_run_sync_logs_counts_and_unlinked_cards(self):
        Card.objects.create(name="Counterspell", set_code="DMR", collector_number="45")
        Card.objects.create(name="Ragavan", set_code="MH2", collector_number="", ct_blueprint_id=1001)
        Card.objects.create(name="X", set_code="PLST", collector_number="9")
        cardmarket.run_sync(self.cm)
        log = SyncLog.objects.first()
        self.assertEqual(log.level, SyncLog.Level.OK)
        self.assertEqual(
            log.message,
            "Cardmarket: 4 expansions, 10 printings, 2 priced (guide 2026-11-07), 2 cards linked, 1 not linked",
        )

    def test_run_sync_mentions_failed_expansions(self):
        self.ct.fail_export = {11, 13}
        cardmarket.run_sync(self.cm)
        log = SyncLog.objects.first()
        self.assertEqual(log.level, SyncLog.Level.WARNING)
        self.assertIn("2 expansions not loaded (dmu, neo)", log.message)


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
