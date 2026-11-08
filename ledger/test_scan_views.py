from decimal import Decimal
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from . import catalog, services
from .models import CollectionItem, Expansion, MarketPrice, ScanLine, ScanSession
from .cardtrader import CardTraderError
from .openrouter import OpenRouterError
from .recognition import Recognition
from .tests import LedgerTestCase


IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"
DESKTOP = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"


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


class ScanViewsBase(LedgerTestCase):
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
        self.set_keys(openrouter="or-key", cardtrader="ct-token")
        self.client.defaults["HTTP_USER_AGENT"] = IPHONE

    def set_keys(self, *, openrouter, cardtrader):
        for service, value in ((services.get_openrouter(), openrouter), (services.get_cardtrader(), cardtrader)):
            service.api_token = value
            service.save()

    @property
    def recognizer_calls(self):
        return self.recognizer.calls

    def start(self):
        self.client.post(reverse("ledger:scan_start"), {"expansion": self.dmr.pk, "condition": "NM"})
        return ScanSession.objects.latest("created_at")

    def capture(self, session, capture_id="c1"):
        frame = SimpleUploadedFile("f.jpg", b"\xff\xd8jpeg", content_type="image/jpeg")
        return self.client.post(reverse("ledger:scan_capture", args=[session.uuid]),
                                {"frame": frame, "capture_id": capture_id})


class ScanViewsTest(ScanViewsBase):
    def test_start_page_lists_expansions(self):
        resp = self.client.get(reverse("ledger:scan_start"))
        self.assertContains(resp, "All expansions")
        self.assertContains(resp, "DMR · Dominaria Remastered")

    def test_start_with_expansion_preloads_catalog_and_prices(self):
        resp = self.client.post(reverse("ledger:scan_start"), {"expansion": self.dmr.pk, "condition": "EX"})
        session = ScanSession.objects.latest("created_at")
        self.assertRedirects(resp, reverse("ledger:scan_camera", args=[session.uuid]))
        self.assertEqual((session.expansion, session.default_condition), (self.dmr, "EX"))
        self.assertTrue(MarketPrice.objects.filter(blueprint_ct_id=1201).exists())

    def test_camera_page_renders(self):
        session = self.start()
        resp = self.client.get(reverse("ledger:scan_camera", args=[session.uuid]))
        self.assertContains(resp, reverse("ledger:scan_capture", args=[session.uuid]))
        self.assertContains(resp, "ledger/scan.js")

    def test_capture_returns_toast_fragment(self):
        resp = self.capture(self.start())
        self.assertEqual(resp["X-Scan-Count"], "1")
        self.assertTemplateUsed(resp, "ledger/includes/scan_toast.html")
        self.assertNotIn(b"<html", resp.content)
        self.assertContains(resp, "Added · 1 copy")
        self.assertContains(resp, "Counterspell")
        self.assertContains(resp, "DMR 45 · NM · IT")

    def test_capture_retry_same_id(self):
        session = self.start()
        self.capture(session, "same")
        self.assertEqual(self.capture(session, "same")["X-Scan-Count"], "1")

    def test_capture_openrouter_error_502(self):
        session = self.start()
        self.recognizer.error = OpenRouterError("down")
        resp = self.capture(session)
        self.assertEqual(resp.status_code, 502)
        self.assertContains(resp, "Couldn&#x27;t recognise", status_code=502)
        self.assertEqual(ScanLine.objects.count(), 0)

    def test_capture_uncertain_then_resolve(self):
        session = self.start()
        self.recognizer.result = Recognition(name="Counterspell", language="")
        resp = self.capture(session)
        line = ScanLine.objects.get(session=session)
        self.assertTrue(line.needs_confirmation)
        self.assertIn(reverse("ledger:scan_resolve", args=[line.uuid]), resp["X-Scan-Redirect"])
        page = self.client.get(resp["X-Scan-Redirect"])
        self.assertContains(page, "Which one is it?")
        self.assertContains(page, "Counterspell")
        frame = self.client.get(reverse("ledger:scan_frame", args=[line.uuid]))
        self.assertEqual(frame["Content-Type"], "image/jpeg")
        camera = reverse("ledger:scan_camera", args=[session.uuid])
        resp = self.client.post(reverse("ledger:scan_resolve", args=[line.uuid]),
                                {"blueprint": 1201, "language": "IT", "next": camera})
        self.assertRedirects(resp, camera)
        line = ScanLine.objects.get(session=session)
        self.assertFalse(line.needs_confirmation)

    def test_manual_search_adds_line(self):
        session = self.start()
        url = reverse("ledger:scan_search", args=[session.uuid])
        self.assertContains(self.client.get(url, {"q": "counter"}), "Counterspell")
        self.client.post(url, {"blueprint": 1201, "language": "EN"})
        self.assertEqual(ScanLine.objects.get(session=session).quantity, 1)

    def test_toast_foil_and_undo_forms(self):
        session = self.start()
        self.capture(session)
        line = ScanLine.objects.get(session=session)
        camera = reverse("ledger:scan_camera", args=[session.uuid])
        url = reverse("ledger:scan_line_update", args=[line.uuid])
        page = self.client.get(camera)
        self.assertContains(page, f'action="{url}"')
        self.assertRedirects(self.client.post(url, {"foil": "1", "next": camera}), camera)
        self.assertTrue(ScanLine.objects.get(pk=line.pk).foil)
        self.client.post(url, {"undo": "1", "next": camera})
        self.assertFalse(ScanLine.objects.filter(pk=line.pk).exists())

    def test_review_threshold_and_save(self):
        session = self.start()
        self.capture(session)
        review = reverse("ledger:scan_review", args=[session.uuid])
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
        resp = self.client.post(reverse("ledger:scan_review", args=[session.uuid]), {"save": "1"}, follow=True)
        self.assertContains(resp, "Some cards still need confirmation.")

    def test_saved_session_redirects(self):
        session = self.start()
        session.status = ScanSession.Status.SAVED
        session.save()
        resp = self.client.get(reverse("ledger:scan_camera", args=[session.uuid]))
        self.assertRedirects(resp, reverse("ledger:collection"))

    def test_collection_links_to_scan(self):
        self.assertContains(self.client.get(reverse("ledger:collection")), reverse("ledger:scan_start"))


class ReviewFixesViewsTest(ScanViewsBase):
    def test_retry_after_merge_adds_once(self):  # C1
        session = self.start()
        self.capture(session, "a")
        self.capture(session, "b")
        resp = self.capture(session, "b")
        self.assertEqual(resp["X-Scan-Count"], "2")
        self.assertEqual(ScanLine.objects.get(session=session).quantity, 2)
        self.assertEqual(self.recognizer_calls, 2)

    def test_capture_in_progress_is_not_reprocessed(self):  # I1
        from .models import ScanCapture

        session = self.start()
        ScanCapture.objects.create(session=session, capture_id="busy")
        resp = self.capture(session, "busy")
        self.assertContains(resp, "Still recognising", status_code=202)
        self.assertEqual(self.recognizer_calls, 0)

    def test_failed_capture_can_be_retried_with_same_id(self):  # I1
        session = self.start()
        self.recognizer.error = OpenRouterError("down")
        self.assertEqual(self.capture(session, "x").status_code, 502)
        self.recognizer.error = None
        self.assertContains(self.capture(session, "x"), "Added · 1 copy")

    def test_search_other_expansion_loads_it(self):  # I2 / I3
        session = self.start()  # DMR locked
        url = reverse("ledger:scan_search", args=[session.uuid])
        resp = self.client.get(url, {"q": "sheoldred", "set": "dmu"})
        self.assertContains(resp, "Sheoldred, the Apocalypse")

    def test_capture_on_closed_session_sends_phone_to_collection(self):
        session = self.start()
        session.status = ScanSession.Status.SAVED
        session.save()
        resp = self.capture(session)
        self.assertEqual(resp["X-Scan-Redirect"], reverse("ledger:collection"))
        self.assertEqual(self.recognizer_calls, 0)


class PhoneOnlyCameraTest(ScanViewsBase):
    def test_desktop_gets_qr_of_this_session_instead_of_camera(self):
        session = self.start()
        camera = reverse("ledger:scan_camera", args=[session.uuid])
        resp = self.client.get(camera, HTTP_USER_AGENT=DESKTOP, secure=True)
        self.assertContains(resp, "Continue on your phone")
        self.assertContains(resp, "<svg")
        self.assertContains(resp, f"https://testserver{camera}")
        self.assertNotContains(resp, "ledger/scan.js")
        self.assertContains(resp, reverse("ledger:scan_review", args=[session.uuid]))
        self.assertNotContains(resp, "needs HTTPS")

    def test_desktop_qr_warns_when_not_https(self):
        session = self.start()
        resp = self.client.get(reverse("ledger:scan_camera", args=[session.uuid]), HTTP_USER_AGENT=DESKTOP)
        self.assertContains(resp, "needs HTTPS")

    def test_android_phone_gets_the_camera(self):
        session = self.start()
        android = "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 Chrome/140.0 Mobile Safari/537.36"
        resp = self.client.get(reverse("ledger:scan_camera", args=[session.uuid]), HTTP_USER_AGENT=android)
        self.assertContains(resp, "ledger/scan.js")

    def test_start_page_button_matches_device(self):
        url = reverse("ledger:scan_start")
        self.assertContains(self.client.get(url), "Open camera")
        desktop = self.client.get(url, HTTP_USER_AGENT=DESKTOP)
        self.assertContains(desktop, "Start and continue on phone")
        self.assertNotContains(desktop, "Open on your phone")


class ScanUuidTest(ScanViewsBase):
    def test_scan_urls_use_uuids_not_ids(self):
        session = self.start()
        self.capture(session)
        line = ScanLine.objects.get(session=session)
        camera = reverse("ledger:scan_camera", args=[session.uuid])
        self.assertIn(str(session.uuid), camera)
        self.assertIn(str(line.uuid), reverse("ledger:scan_line_update", args=[line.uuid]))
        self.assertEqual(self.client.get(f"/scan/{session.pk}/").status_code, 404)
        self.assertContains(self.client.get(camera), str(line.uuid))


class ScanErrorMessagesTest(ScanViewsBase):
    def test_capture_without_openrouter_key_says_so(self):
        session = self.start()
        self.set_keys(openrouter="", cardtrader="ct-token")
        resp = self.capture(session)
        self.assertContains(resp, "OpenRouter is not connected", status_code=503)
        self.assertContains(resp, reverse("accounts:profile") + "#openrouter", status_code=503)
        self.assertEqual((self.recognizer_calls, ScanLine.objects.count()), (0, 0))

    def test_capture_without_cardtrader_token_says_so(self):
        session = self.start()
        self.set_keys(openrouter="or-key", cardtrader="")
        resp = self.capture(session)
        self.assertContains(resp, "CardTrader is not connected", status_code=503)
        self.assertContains(resp, reverse("accounts:profile") + "#cardtrader", status_code=503)
        self.assertEqual(self.recognizer_calls, 0)

    def test_capture_error_shows_the_reason(self):
        session = self.start()
        self.recognizer.error = OpenRouterError("OpenRouter failed (429): rate limited")
        resp = self.capture(session)
        self.assertContains(resp, "rate limited", status_code=502)
        self.assertContains(resp, "take the photo again", status_code=502)

    def test_start_page_warns_about_missing_keys(self):
        self.set_keys(openrouter="", cardtrader="")
        Expansion.objects.all().delete()
        no_token = CardTraderError("CardTrader API token is not configured.")
        with mock.patch.object(services, "make_client", side_effect=no_token):
            resp = self.client.get(reverse("ledger:scan_start"))
        self.assertNotContains(resp, "Could not load the expansions")
        self.assertContains(resp, "OpenRouter is not connected")
        self.assertContains(resp, "CardTrader is not connected")
        self.assertContains(resp, reverse("accounts:profile") + "#openrouter")

    def test_start_page_without_warnings_when_connected(self):
        resp = self.client.get(reverse("ledger:scan_start"))
        self.assertNotContains(resp, "is not connected")


class NoCardTest(ScanViewsBase):
    def test_photo_without_a_card_adds_nothing(self):
        session = self.start()
        self.recognizer.result = Recognition()  # the model saw no card
        resp = self.capture(session, "empty")
        self.assertContains(resp, "No card in the picture")
        self.assertEqual(ScanLine.objects.count(), 0)
        self.assertEqual(self.capture(session, "empty2")["X-Scan-Count"], "0")


class CameraToastTest(ScanViewsBase):
    def test_new_session_camera_shows_no_last_added(self):
        session = self.start()
        resp = self.client.get(reverse("ledger:scan_camera", args=[session.uuid]))
        self.assertNotContains(resp, "Last added")
        self.assertContains(resp, 'id="scan-toast" role="status" aria-live="polite" hidden')

    def test_camera_reopened_shows_last_added_card(self):
        session = self.start()
        self.capture(session)
        resp = self.client.get(reverse("ledger:scan_camera", args=[session.uuid]))
        self.assertContains(resp, "Last added")
        self.assertContains(resp, "Counterspell")
