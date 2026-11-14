from decimal import Decimal
from io import StringIO
from unittest import mock

import httpx
from django.core.management import call_command
from django.test import TestCase

from . import catalog_sync, services
from .models import Blueprint, Card, CardmarketPrice, Category, Expansion, SyncLog
from .test_cardmarket import CardmarketTestCase
from .tests import FakeCardTrader


class SyncTest(CardmarketTestCase):
    def test_sync_loads_every_mtg_expansion_and_blueprint(self):
        result = self.sync()
        self.assertEqual(set(Expansion.objects.values_list("code", flat=True)), {"mh2", "neo", "dmr", "dmu"})
        self.assertEqual(Blueprint.objects.count(), 10)
        self.assertFalse(Expansion.objects.filter(blueprints_synced_at__isnull=True).exists())
        rag = Blueprint.objects.get(ct_id=1001)
        self.assertEqual((rag.name, rag.expansion.code, rag.collector_number), ("Ragavan, Nimble Pilferer", "mh2", "138"))
        self.assertEqual((rag.cm_price_id, rag.image_url), (555001, "https://img/1001.jpg"))
        self.assertEqual(rag.image_large_url, "https://cardtrader.com/uploads/show_1001.jpg")
        self.assertEqual((result.expansions, result.blueprints, result.priced), (4, 10, 2))

    def test_sync_stores_categories_and_properties(self):
        self.sync()
        self.assertEqual(Category.objects.get(ct_id=40).name, "Playmats")
        box = Blueprint.objects.get(ct_id=1390)
        self.assertEqual((box.category.ct_id, box.editable_properties), (4, ["mtg_language"]))
        self.assertTrue(box.is_product)
        self.assertFalse(Blueprint.objects.get(ct_id=1001).is_product)

    def test_sync_marks_existing_cards_as_products(self):
        card = Card.objects.create(name="Dominaria United Draft Booster Box", set_code="DMU",
                                   collector_number="CT1390", ct_blueprint_id=1390)
        self.sync()
        card.refresh_from_db()
        self.assertTrue(card.is_product)

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

    def test_sync_written_in_batches(self):
        with mock.patch.object(catalog_sync, "BATCH", 3):
            result = self.sync()
        self.assertEqual((Blueprint.objects.count(), result.blueprints, result.priced), (10, 10, 2))
        self.assertEqual(CardmarketPrice.objects.count(), 3)
        self.assertEqual(Blueprint.objects.get(ct_id=1390).category.ct_id, 4)
        self.assertEqual(CardmarketPrice.objects.get(pk=555201).trend, Decimal("1.20"))

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
        self.assertIsNone(catalog_sync.run_sync(self.cm))
        self.assertEqual(CardmarketPrice.objects.get(pk=555001).trend, Decimal("1.00"))
        self.assertFalse(Blueprint.objects.exists())
        log = SyncLog.objects.first()
        self.assertEqual(log.level, SyncLog.Level.WARNING)
        self.assertIn("Catalog sync failed", log.message)

    def test_malformed_guide_changes_nothing(self):
        self.guide.raw = b"not json"
        self.assertIsNone(catalog_sync.run_sync(self.cm))
        self.assertFalse(Blueprint.objects.exists())

    def test_cardtrader_expansions_failure_changes_nothing(self):
        with mock.patch.object(FakeCardTrader, "handler", lambda s, r: httpx.Response(500, json={})):
            self.assertIsNone(catalog_sync.run_sync(self.cm))
        self.assertFalse(CardmarketPrice.objects.exists())
        self.assertIn("Catalog sync failed", SyncLog.objects.first().message)

    def test_missing_cardtrader_token_logs_warning(self):
        services.make_client.side_effect = services.CardTraderError("CardTrader API token is not configured.")
        self.assertIsNone(catalog_sync.run_sync(self.cm))
        self.assertIn("token is not configured", SyncLog.objects.first().message)

    def test_unexpected_failure_is_logged_and_raised(self):
        with mock.patch.object(CardmarketPrice.objects, "bulk_create", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                catalog_sync.run_sync(self.cm)
        log = SyncLog.objects.first()
        self.assertEqual(log.level, SyncLog.Level.WARNING)
        self.assertEqual(log.message, "Catalog sync failed: RuntimeError: db down")

    def test_run_sync_logs_counts_and_unlinked_cards(self):
        Card.objects.create(name="Counterspell", set_code="DMR", collector_number="45")
        Card.objects.create(name="Ragavan", set_code="MH2", collector_number="", ct_blueprint_id=1001)
        Card.objects.create(name="X", set_code="PLST", collector_number="9")
        catalog_sync.run_sync(self.cm)
        log = SyncLog.objects.first()
        self.assertEqual(log.level, SyncLog.Level.OK)
        self.assertEqual(
            log.message,
            "Catalog sync: 4 expansions, 10 printings, 2 priced (guide 2026-11-07), 2 cards linked, 1 not linked",
        )

    def test_run_sync_mentions_failed_expansions(self):
        self.ct.fail_export = {11, 13}
        catalog_sync.run_sync(self.cm)
        log = SyncLog.objects.first()
        self.assertEqual(log.level, SyncLog.Level.WARNING)
        self.assertIn("2 expansions not loaded (dmu, neo)", log.message)


class SyncCommandTest(TestCase):
    def test_command_only_enqueues(self):
        from . import tasks

        with mock.patch.object(tasks.sync_catalog, "delay", return_value=mock.Mock(id="abc")) as delay, \
                mock.patch.object(catalog_sync, "run_sync") as run:
            out = StringIO()
            call_command("sync_catalog", stdout=out)
        delay.assert_called_once_with()
        run.assert_not_called()
        self.assertIn("Catalog sync queued (task abc)", out.getvalue())

    def test_task_runs_sync(self):
        from . import tasks

        with mock.patch.object(catalog_sync, "run_sync") as run:
            tasks.sync_catalog.apply()
        run.assert_called_once_with()

    def test_no_beat_schedule(self):
        from config.celery import app

        self.assertFalse(app.conf.beat_schedule)
