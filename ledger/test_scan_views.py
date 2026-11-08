from decimal import Decimal
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from . import catalog, services
from .models import CollectionItem, Expansion, MarketPrice, ScanLine, ScanSession
from .openrouter import OpenRouterError
from .recognition import Recognition
from .tests import LedgerTestCase


class FakeRecognizer:
    def __init__(self):
        self.result = Recognition(name="Counterspell", set_code="dmr", collector_number="045", language="IT")
        self.error = None
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def recognize_card(self, image):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class ScanViewsTest(LedgerTestCase):
    def setUp(self):
        super().setUp()
        api = self.ct.client()
        catalog.sync_expansions(api)
        self.dmr = Expansion.objects.get(code="dmr")
        catalog.ensure_blueprints(api, self.dmr)
        self.recognizer = FakeRecognizer()
        for target, kwargs in (
            (services, {"make_openrouter": mock.Mock(return_value=self.recognizer)}),
        ):
            patcher = mock.patch.multiple(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        price = mock.patch("ledger.pricing.price_for", return_value=Decimal("5.00"))
        price.start()
        self.addCleanup(price.stop)

    @property
    def recognizer_calls(self):
        return self.recognizer.calls

    def start(self):
        self.client.post(reverse("ledger:scan_start"), {"expansion": self.dmr.pk, "condition": "NM"})
        return ScanSession.objects.latest("created_at")

    def capture(self, session, capture_id="c1"):
        frame = SimpleUploadedFile("f.jpg", b"\xff\xd8jpeg", content_type="image/jpeg")
        return self.client.post(reverse("ledger:scan_capture", args=[session.pk]),
                                {"frame": frame, "capture_id": capture_id})

    def test_start_page_lists_expansions(self):
        resp = self.client.get(reverse("ledger:scan_start"))
        self.assertContains(resp, "All expansions")
        self.assertContains(resp, "DMR · Dominaria Remastered")

    def test_start_with_expansion_preloads_catalog_and_prices(self):
        resp = self.client.post(reverse("ledger:scan_start"), {"expansion": self.dmr.pk, "condition": "EX"})
        session = ScanSession.objects.latest("created_at")
        self.assertRedirects(resp, reverse("ledger:scan_camera", args=[session.pk]))
        self.assertEqual((session.expansion, session.default_condition), (self.dmr, "EX"))
        self.assertTrue(MarketPrice.objects.filter(blueprint_ct_id=1201).exists())

    def test_camera_page_renders(self):
        session = self.start()
        resp = self.client.get(reverse("ledger:scan_camera", args=[session.pk]))
        self.assertContains(resp, reverse("ledger:scan_capture", args=[session.pk]))
        self.assertContains(resp, "ledger/scan.js")

    def test_capture_added_json(self):
        data = self.capture(self.start()).json()
        self.assertEqual((data["status"], data["count"], data["merged"]), ("added", 1, False))
        self.assertEqual((data["line"]["name"], data["line"]["number"], data["line"]["language"]),
                         ("Counterspell", "45", "IT"))

    def test_capture_retry_same_id(self):
        session = self.start()
        self.capture(session, "same")
        self.assertEqual(self.capture(session, "same").json()["count"], 1)

    def test_capture_openrouter_error_502(self):
        session = self.start()
        self.recognizer.error = OpenRouterError("down")
        resp = self.capture(session)
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.json()["status"], "error")
        self.assertEqual(ScanLine.objects.count(), 0)

    def test_capture_uncertain_then_resolve(self):
        session = self.start()
        self.recognizer.result = Recognition(name="Counterspell", language="")
        data = self.capture(session).json()
        self.assertEqual(data["status"], "uncertain")
        line = ScanLine.objects.get(pk=data["line_id"])
        page = self.client.get(data["resolve_url"])
        self.assertContains(page, "Which one is it?")
        self.assertContains(page, "Counterspell")
        frame = self.client.get(reverse("ledger:scan_frame", args=[line.pk]))
        self.assertEqual(frame["Content-Type"], "image/jpeg")
        camera = reverse("ledger:scan_camera", args=[session.pk])
        resp = self.client.post(reverse("ledger:scan_resolve", args=[line.pk]),
                                {"blueprint": 1201, "language": "IT", "next": camera})
        self.assertRedirects(resp, camera)
        line = ScanLine.objects.get(session=session)
        self.assertFalse(line.needs_confirmation)

    def test_manual_search_adds_line(self):
        session = self.start()
        url = reverse("ledger:scan_search", args=[session.pk])
        self.assertContains(self.client.get(url, {"q": "counter"}), "Counterspell")
        self.client.post(url, {"blueprint": 1201, "language": "EN"})
        self.assertEqual(ScanLine.objects.get(session=session).quantity, 1)

    def test_line_update_json_and_undo(self):
        session = self.start()
        line_id = self.capture(session).json()["line"]["id"]
        url = reverse("ledger:scan_line_update", args=[line_id])
        resp = self.client.post(url, {"foil": "1"}, HTTP_ACCEPT="application/json")
        self.assertEqual(resp.json(), {"ok": True, "count": 1, "line_id": line_id})
        self.assertTrue(ScanLine.objects.get(pk=line_id).foil)
        resp = self.client.post(url, {"undo": "1"}, HTTP_ACCEPT="application/json")
        self.assertEqual(resp.json()["count"], 0)

    def test_review_threshold_and_save(self):
        session = self.start()
        self.capture(session)
        review = reverse("ledger:scan_review", args=[session.pk])
        self.client.post(review, {"threshold": "3.00"})
        session.refresh_from_db()
        self.assertEqual(session.sale_threshold, Decimal("3.00"))
        page = self.client.get(review)
        self.assertContains(page, "1 card to save")
        self.assertContains(page, "For sale")
        resp = self.client.post(review, {"save": "1"}, follow=True)
        self.assertRedirects(resp, reverse("ledger:collection"))
        self.assertContains(resp, "Saved 1 cards, 1 queued for sale.")
        self.assertTrue(CollectionItem.objects.filter(card__ct_blueprint_id=1201).exists())

    def test_save_blocked_message(self):
        session = self.start()
        self.recognizer.result = Recognition(name="Counterspell", language="")
        self.capture(session)
        resp = self.client.post(reverse("ledger:scan_review", args=[session.pk]), {"save": "1"}, follow=True)
        self.assertContains(resp, "Some cards still need confirmation.")

    def test_saved_session_redirects(self):
        session = self.start()
        session.status = ScanSession.Status.SAVED
        session.save()
        resp = self.client.get(reverse("ledger:scan_camera", args=[session.pk]))
        self.assertRedirects(resp, reverse("ledger:collection"))

    def test_collection_links_to_scan(self):
        self.assertContains(self.client.get(reverse("ledger:collection")), reverse("ledger:scan_start"))


class ReviewFixesViewsTest(ScanViewsTest):
    def test_retry_after_merge_adds_once(self):  # C1
        session = self.start()
        self.capture(session, "a")
        self.capture(session, "b")
        data = self.capture(session, "b").json()
        self.assertEqual((data["status"], data["count"]), ("added", 2))
        self.assertEqual(self.recognizer_calls, 2)

    def test_capture_in_progress_is_not_reprocessed(self):  # I1
        from .models import ScanCapture

        session = self.start()
        ScanCapture.objects.create(session=session, capture_id="busy")
        resp = self.capture(session, "busy")
        self.assertEqual((resp.status_code, resp.json()["status"]), (202, "pending"))
        self.assertEqual(self.recognizer_calls, 0)

    def test_failed_capture_can_be_retried_with_same_id(self):  # I1
        session = self.start()
        self.recognizer.error = OpenRouterError("down")
        self.assertEqual(self.capture(session, "x").status_code, 502)
        self.recognizer.error = None
        self.assertEqual(self.capture(session, "x").json()["status"], "added")

    def test_search_other_expansion_loads_it(self):  # I2 / I3
        session = self.start()  # DMR locked
        url = reverse("ledger:scan_search", args=[session.pk])
        resp = self.client.get(url, {"q": "sheoldred", "set": "dmu"})
        self.assertContains(resp, "Sheoldred, the Apocalypse")
