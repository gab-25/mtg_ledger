import json
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from . import catalog, services
from .cardtrader import CardTraderError
from .models import CollectionItem, Expansion, MarketPrice, ScanCapture, ScanLine, ScanSession
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


class ScanApiBase(LedgerTestCase):
    def setUp(self):
        super().setUp()
        api = self.ct.client()
        catalog.sync_expansions(api)
        self.dmr = Expansion.objects.get(code="dmr")
        catalog.ensure_blueprints(api, self.dmr)
        self.recognizer = FakeRecognizer()
        patcher = mock.patch.object(services, "make_openrouter", mock.Mock(return_value=self.recognizer))
        patcher.start()
        self.addCleanup(patcher.stop)
        price = mock.patch("ledger.pricing.price_for", return_value=Decimal("5.00"))
        price.start()
        self.addCleanup(price.stop)
        self.set_keys(openrouter="or-key", cardtrader="ct-token")
        self.client.defaults["HTTP_USER_AGENT"] = IPHONE

    def set_keys(self, *, openrouter, cardtrader):
        override = self.settings(OPENROUTER_API_KEY=openrouter)
        override.enable()
        self.addCleanup(override.disable)
        service = services.get_cardtrader()
        service.api_token = cardtrader
        service.save()

    def call(self, method, name, *args, data=None, **extra):
        body = json.dumps(data) if data is not None else ""
        return getattr(self.client, method)(reverse(f"ledger:{name}", args=args), body,
                                            content_type="application/json", **extra)

    def start(self, expansion_id=None, condition="NM"):
        resp = self.call("post", "scan_api_sessions",
                         data={"expansion_id": expansion_id or self.dmr.pk, "condition": condition})
        self.assertEqual(resp.status_code, 201, resp.content)
        return ScanSession.objects.get(uuid=resp.json()["session"]["uuid"])

    def capture(self, session, capture_id="c1"):
        frame = SimpleUploadedFile("f.jpg", b"\xff\xd8jpeg", content_type="image/jpeg")
        return self.client.post(reverse("ledger:scan_api_captures", args=[session.uuid]),
                                {"frame": frame, "capture_id": capture_id})

    def assertError(self, resp, status, code):
        self.assertEqual(resp.status_code, status, resp.content)
        body = resp.json()
        self.assertEqual(body["code"], code)
        self.assertIsInstance(body["error"], str)
        return body


class SetupAndSessionTest(ScanApiBase):
    def test_setup_lists_expansions_and_choices(self):
        body = self.call("get", "scan_api_setup").json()
        self.assertIn({"id": self.dmr.pk, "code": "dmr", "name": "Dominaria Remastered"}, body["expansions"])
        self.assertIn({"code": "NM", "label": "Near Mint"}, body["conditions"])
        self.assertIn({"code": "IT", "label": "Italian"}, body["languages"])
        self.assertEqual(body["defaults"], {"expansion_id": None, "condition": "NM"})
        self.assertEqual((body["missing"], body["warnings"]), ([], []))

    def test_setup_defaults_follow_the_last_session(self):
        self.start(condition="EX")
        self.assertEqual(self.call("get", "scan_api_setup").json()["defaults"],
                         {"expansion_id": self.dmr.pk, "condition": "EX"})

    def test_setup_syncs_expansions_when_empty(self):
        Expansion.objects.all().delete()
        body = self.call("get", "scan_api_setup").json()
        self.assertTrue(any(e["code"] == "dmr" for e in body["expansions"]))

    def test_start_preloads_catalog_and_prices(self):
        resp = self.call("post", "scan_api_sessions", data={"expansion_id": self.dmr.pk, "condition": "EX"})
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        session = ScanSession.objects.get(uuid=body["session"]["uuid"])
        self.assertEqual((session.expansion, session.default_condition), (self.dmr, "EX"))
        self.assertEqual(body["session"]["expansion"]["code"], "dmr")
        self.assertEqual((body["session"]["status"], body["session"]["count"], body["warnings"]), ("open", 0, []))
        self.assertTrue(MarketPrice.objects.filter(blueprint_ct_id=1201).exists())

    def test_start_all_expansions(self):
        resp = self.call("post", "scan_api_sessions", data={"expansion_id": None, "condition": "NM"})
        self.assertEqual(resp.status_code, 201)
        self.assertIsNone(resp.json()["session"]["expansion"])

    def test_start_unknown_expansion_404(self):
        self.assertError(self.call("post", "scan_api_sessions", data={"expansion_id": 999999}), 404, "not_found")

    def test_session_detail_shows_last_added_line(self):
        session = self.start()
        self.assertIsNone(self.call("get", "scan_api_session", session.uuid).json()["session"]["last_line"])
        self.capture(session)
        body = self.call("get", "scan_api_session", session.uuid).json()["session"]
        self.assertEqual(body["count"], 1)
        line = body["last_line"]
        self.assertEqual(line["blueprint"]["id"], 1201)
        self.assertEqual(line["blueprint"]["expansion"]["code"], "dmr")
        self.assertEqual((line["language"], line["condition"], line["price"]), ("IT", "NM", "5.00"))

    def test_saved_session_is_still_readable(self):
        session = self.start()
        session.status = ScanSession.Status.SAVED
        session.save()
        resp = self.call("get", "scan_api_session", session.uuid)
        self.assertEqual(resp.json()["session"]["status"], "saved")

    def test_unknown_session_404(self):
        self.assertError(self.call("get", "scan_api_session", "00000000-0000-0000-0000-000000000000"),
                         404, "not_found")


class CaptureTest(ScanApiBase):
    def test_capture_adds_line(self):
        resp = self.capture(self.start())
        body = resp.json()
        self.assertEqual((body["result"], body["message"], body["count"]), ("added", "Added · 1 copy", 1))
        self.assertEqual(body["line"]["blueprint"]["name"], "Counterspell")

    def test_capture_retry_same_id(self):
        session = self.start()
        self.capture(session, "same")
        body = self.capture(session, "same").json()
        self.assertEqual((body["result"], body["count"]), ("added", 1))
        self.assertEqual(self.recognizer.calls, 1)

    def test_retry_after_merge_adds_once(self):
        session = self.start()
        self.capture(session, "a")
        self.capture(session, "b")
        body = self.capture(session, "b").json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["message"], "Added · 2 copies")
        self.assertEqual(ScanLine.objects.get(session=session).quantity, 2)
        self.assertEqual(self.recognizer.calls, 2)

    def test_capture_in_progress_is_busy(self):
        session = self.start()
        ScanCapture.objects.create(session=session, capture_id="busy")
        resp = self.capture(session, "busy")
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.json()["result"], "busy")
        self.assertIn("Still recognising", resp.json()["message"])
        self.assertEqual(self.recognizer.calls, 0)

    def test_undone_capture_is_handled(self):
        session = self.start()
        line_uuid = self.capture(session, "u").json()["line"]["uuid"]
        self.call("post", "scan_api_undo", line_uuid)
        body = self.capture(session, "u").json()
        self.assertEqual((body["result"], body["line"], body["count"]), ("handled", None, 0))

    def test_failed_capture_can_be_retried_with_same_id(self):
        session = self.start()
        self.recognizer.error = OpenRouterError("down")
        self.assertError(self.capture(session, "x"), 502, "service_error")
        self.recognizer.error = None
        self.assertEqual(self.capture(session, "x").json()["message"], "Added · 1 copy")

    def test_capture_error_shows_the_reason(self):
        session = self.start()
        self.recognizer.error = OpenRouterError("OpenRouter failed (429): rate limited")
        body = self.assertError(self.capture(session), 502, "service_error")
        self.assertIn("rate limited", body["error"])
        self.assertIn("take the photo again", body["error"])
        self.assertEqual(ScanLine.objects.count(), 0)

    def test_photo_without_a_card_adds_nothing(self):
        session = self.start()
        self.recognizer.result = Recognition()
        body = self.capture(session, "empty").json()
        self.assertEqual((body["result"], body["line"], body["count"]), ("no_card", None, 0))
        self.assertIn("No card in the picture", body["message"])
        self.assertEqual(ScanLine.objects.count(), 0)
        self.assertFalse(ScanCapture.objects.filter(capture_id="empty").exists())

    def test_missing_or_oversized_photo(self):
        session = self.start()
        resp = self.client.post(reverse("ledger:scan_api_captures", args=[session.uuid]), {"capture_id": "x"})
        self.assertError(resp, 400, "invalid")

    def test_capture_on_closed_session_409(self):
        session = self.start()
        session.status = ScanSession.Status.SAVED
        session.save()
        self.assertError(self.capture(session), 409, "closed")
        self.assertEqual(self.recognizer.calls, 0)


class MissingServicesTest(ScanApiBase):
    def test_capture_without_openrouter_key_logs_it_on_the_server(self):
        session = self.start()
        self.set_keys(openrouter="", cardtrader="ct-token")
        with self.assertLogs("ledger.scan_api", "WARNING") as logs:
            resp = self.capture(session)
        self.assertIn("OPENROUTER_API_KEY", logs.output[0])
        body = self.assertError(resp, 503, "service_missing")
        self.assertEqual(body["error"], "Card recognition is not available right now — try again later")
        self.assertNotIn("help_url", body)
        self.assertEqual((self.recognizer.calls, ScanLine.objects.count()), (0, 0))

    def test_capture_without_cardtrader_token_says_so(self):
        session = self.start()
        self.set_keys(openrouter="or-key", cardtrader="")
        body = self.assertError(self.capture(session), 503, "service_missing")
        self.assertIn("CardTrader is not connected", body["error"])
        self.assertEqual(body["help_url"], reverse("accounts:profile") + "#cardtrader")
        self.assertEqual(self.recognizer.calls, 0)

    def test_setup_lists_missing_services(self):
        self.set_keys(openrouter="", cardtrader="")
        Expansion.objects.all().delete()
        no_token = CardTraderError("CardTrader API token is not configured.")
        with mock.patch.object(services, "make_client", side_effect=no_token), \
                self.assertNoLogs("ledger.scan_api", "WARNING"):
            body = self.call("get", "scan_api_setup").json()
        self.assertEqual(body["warnings"], [])
        self.assertEqual(len(body["missing"]), 1)
        self.assertIn("CardTrader is not connected", body["missing"][0]["message"])
        self.assertEqual(body["missing"][0]["url"], reverse("accounts:profile") + "#cardtrader")
        self.assertNotIn("OpenRouter", json.dumps(body))


class ConfirmTest(ScanApiBase):
    def uncertain(self, session):
        self.recognizer.result = Recognition(name="Counterspell", language="")
        return self.capture(session).json()

    def test_uncertain_capture_then_confirm(self):
        session = self.start()
        body = self.uncertain(session)
        self.assertEqual(body["result"], "confirm")
        line = body["line"]
        self.assertTrue(line["needs_confirmation"])
        self.assertEqual([c["id"] for c in line["candidates"]], [1201])
        frame = self.client.get(line["frame_url"])
        self.assertEqual(frame["Content-Type"], "image/jpeg")
        resp = self.call("post", "scan_api_confirm", line["uuid"],
                         data={"blueprint_id": 1201, "language": "IT", "condition": "EX", "foil": True})
        confirmed = resp.json()["line"]
        self.assertFalse(confirmed["needs_confirmation"])
        self.assertEqual((confirmed["language"], confirmed["condition"], confirmed["foil"]), ("IT", "EX", True))
        self.assertIsNone(confirmed["frame_url"])
        self.assertEqual(resp.json()["count"], 1)

    def test_confirm_needs_card_and_language(self):
        session = self.start()
        line = self.uncertain(session)["line"]
        body = self.assertError(self.call("post", "scan_api_confirm", line["uuid"], data={"blueprint_id": 1201}),
                                400, "invalid")
        self.assertEqual(body["error"], "Pick a card and its language.")

    def test_confirmed_line_is_returned_unchanged(self):
        session = self.start()
        line = self.capture(session).json()["line"]
        resp = self.call("post", "scan_api_confirm", line["uuid"], data={"blueprint_id": 1301, "language": "EN"})
        self.assertEqual(resp.json()["line"], line)

    def test_save_blocked_while_cards_need_confirmation(self):
        session = self.start()
        self.uncertain(session)
        body = self.assertError(self.call("post", "scan_api_save", session.uuid), 409, "blocked")
        self.assertEqual(body["error"], "Some cards still need confirmation.")
        review = self.call("get", "scan_api_review", session.uuid).json()
        self.assertEqual((len(review["pending"]), len(review["lines"])), (1, 0))


class SearchTest(ScanApiBase):
    def test_manual_search_adds_line(self):
        session = self.start()
        body = self.client.get(reverse("ledger:scan_api_search", args=[session.uuid]), {"q": "counter"}).json()
        self.assertEqual([b["name"] for b in body["results"]], ["Counterspell"])
        self.assertEqual(body["where"], "dmr")
        resp = self.call("post", "scan_api_manual", session.uuid,
                         data={"blueprint_id": 1201, "language": "EN", "foil": False})
        self.assertEqual(resp.json()["count"], 1)
        self.assertEqual(ScanLine.objects.get(session=session).quantity, 1)

    def test_manual_needs_card_and_language(self):
        session = self.start()
        self.assertError(self.call("post", "scan_api_manual", session.uuid, data={"blueprint_id": 1201}),
                         400, "invalid")

    def test_search_other_expansion_loads_it(self):
        session = self.start()  # DMR locked
        resp = self.client.get(reverse("ledger:scan_api_search", args=[session.uuid]), {"q": "sheoldred", "set": "dmu"})
        body = resp.json()
        self.assertEqual([b["name"] for b in body["results"]], ["Sheoldred, the Apocalypse"])
        self.assertEqual(body["where"], "dmu")

    def test_search_all_expansions(self):
        session = self.start()
        resp = self.client.get(reverse("ledger:scan_api_search", args=[session.uuid]), {"q": "counter", "set": "all"})
        self.assertIsNone(resp.json()["where"])

    def test_search_reports_load_failures(self):
        session = self.start()
        self.ct.fail_export.add(13)
        resp = self.client.get(reverse("ledger:scan_api_search", args=[session.uuid]), {"q": "x", "set": "dmu"})
        self.assertTrue(resp.json()["warnings"])


class PhoneTest(ScanApiBase):
    def test_desktop_gets_the_phone_url_of_this_session(self):
        session = self.start()
        body = self.call("get", "scan_api_session", session.uuid, HTTP_USER_AGENT=DESKTOP, secure=True).json()
        data = body["session"]
        self.assertFalse(data["is_phone"])
        self.assertEqual(data["phone_url"], f"https://testserver/scan/{session.uuid}/")
        self.assertNotIn("phone_qr_svg", data)
        self.assertTrue(data["phone_secure"])

    def test_desktop_qr_warns_when_not_https(self):
        session = self.start()
        data = self.call("get", "scan_api_session", session.uuid, HTTP_USER_AGENT=DESKTOP).json()["session"]
        self.assertFalse(data["phone_secure"])

    def test_android_is_a_phone(self):
        session = self.start()
        android = "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 Chrome/140.0 Mobile Safari/537.36"
        data = self.call("get", "scan_api_session", session.uuid, HTTP_USER_AGENT=android).json()["session"]
        self.assertTrue(data["is_phone"])


class ReviewTest(ScanApiBase):
    def test_threshold_and_save(self):
        session = self.start()
        self.capture(session)
        body = self.call("put", "scan_api_threshold", session.uuid, data={"threshold": "3,00"}).json()
        self.assertEqual(body["session"]["sale_threshold"], "3.00")
        self.assertEqual((body["copies"], body["unique"], body["listed"]), (1, 1, 1))
        self.assertTrue(body["lines"][0]["listed"])
        resp = self.call("post", "scan_api_save", session.uuid)
        self.assertEqual(resp.json(), {"saved": 1, "listed": 1, "redirect": "/"})
        self.assertTrue(CollectionItem.objects.filter(card__ct_blueprint_id=1201).exists())
        self.assertContains(self.client.get("/"), "Saved 1 cards, 1 queued for sale.")

    def test_threshold_cleared(self):
        session = self.start()
        self.call("put", "scan_api_threshold", session.uuid, data={"threshold": "3"})
        body = self.call("put", "scan_api_threshold", session.uuid, data={"threshold": ""}).json()
        self.assertIsNone(body["session"]["sale_threshold"])

    def test_threshold_must_be_a_price(self):
        session = self.start()
        body = self.assertError(self.call("put", "scan_api_threshold", session.uuid, data={"threshold": "abc"}),
                                400, "invalid")
        self.assertEqual(body["error"], "Enter the threshold as a price, e.g. 2.50.")

    def test_closed_session_rejects_changes(self):
        session = self.start()
        line = self.capture(session).json()["line"]
        self.call("post", "scan_api_save", session.uuid)
        self.assertError(self.call("post", "scan_api_save", session.uuid), 409, "closed")
        self.assertError(self.call("put", "scan_api_threshold", session.uuid, data={"threshold": "1"}),
                         409, "closed")
        self.assertError(self.call("post", "scan_api_manual", session.uuid,
                                   data={"blueprint_id": 1201, "language": "EN"}), 409, "closed")
        self.assertError(self.call("patch", "scan_api_line", line["uuid"], data={"foil": True}), 409, "closed")
        self.assertError(self.call("post", "scan_api_undo", line["uuid"]), 409, "closed")
        self.assertError(self.call("post", "scan_api_confirm", line["uuid"], data={}), 409, "closed")
        self.assertEqual(self.call("get", "scan_api_review", session.uuid).status_code, 200)


class LineTest(ScanApiBase):
    def setUp(self):
        super().setUp()
        self.session = self.start()
        self.line = self.capture(self.session).json()["line"]

    def patch(self, data):
        return self.call("patch", "scan_api_line", self.line["uuid"], data=data)

    def test_patch_fields(self):
        body = self.patch({"foil": True, "condition": "EX", "language": "en", "quantity": 3, "for_sale": False}).json()
        line = body["line"]
        self.assertEqual((line["foil"], line["condition"], line["language"], line["quantity"], line["for_sale"]),
                         (True, "EX", "EN", 3, False))
        self.assertEqual(body["count"], 3)
        self.assertIsNone(self.patch({"for_sale": None}).json()["line"]["for_sale"])

    def test_patch_quantity_zero_deletes(self):
        self.assertEqual(self.patch({"quantity": 0}).json(), {"line": None, "count": 0})

    def test_patch_validation(self):
        for data in ({"foil": "1"}, {"condition": "XX"}, {"language": "KL"}, {"quantity": -1},
                     {"quantity": "2"}, {"quantity": True}, {"for_sale": "yes"}, {"price": "1"}, {}, ["foil"]):
            with self.subTest(data=data):
                self.assertError(self.patch(data), 400, "invalid")
        self.assertError(self.client.patch(reverse("ledger:scan_api_line", args=[self.line["uuid"]]), "{",
                                           content_type="application/json"), 400, "invalid")
        line = ScanLine.objects.get(uuid=self.line["uuid"])
        self.assertEqual((line.foil, line.condition, line.quantity), (False, "NM", 1))

    def test_undo(self):
        self.capture(self.session, "c2")
        body = self.call("post", "scan_api_undo", self.line["uuid"]).json()
        self.assertEqual((body["line"]["quantity"], body["count"]), (1, 1))
        self.assertEqual(self.call("post", "scan_api_undo", self.line["uuid"]).json(), {"line": None, "count": 0})
        self.assertError(self.call("post", "scan_api_undo", self.line["uuid"]), 404, "not_found")


class ProtocolTest(ScanApiBase):
    def test_401_without_login(self):
        self.client.logout()
        session = ScanSession.objects.create()
        for resp in (self.call("get", "scan_api_setup"),
                     self.call("post", "scan_api_sessions", data={}),
                     self.call("get", "scan_api_review", session.uuid)):
            self.assertError(resp, 401, "login")

    def test_403_without_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        resp = client.post(reverse("ledger:scan_api_sessions"), "{}", content_type="application/json")
        self.assertEqual(resp.status_code, 403)
        client.get(reverse("ledger:scan_start"))
        token = client.cookies["csrftoken"].value
        resp = client.post(reverse("ledger:scan_api_sessions"), "{}", content_type="application/json",
                           HTTP_X_CSRFTOKEN=token)
        self.assertEqual(resp.status_code, 201)

    def test_405_on_wrong_method(self):
        session = self.start()
        resp = self.call("get", "scan_api_sessions")
        self.assertError(resp, 405, "invalid")
        self.assertEqual(resp["Allow"], "POST")
        self.assertError(self.call("post", "scan_api_review", session.uuid), 405, "invalid")
        self.assertError(self.call("post", "scan_api_threshold", session.uuid, data={}), 405, "invalid")

    def test_decimals_are_strings(self):
        session = self.start()
        line = self.capture(session).json()["line"]
        self.assertEqual(line["price"], "5.00")


class ScanAppTest(LedgerTestCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        override = self.settings(SCAN_APP_DIR=self.dir)
        override.enable()
        self.addCleanup(override.disable)

    def test_spa_routes_serve_index_and_set_csrf_cookie(self):
        (self.dir / "index.html").write_text("<!doctype html><div id=root></div>")
        for url in ("/scan/", "/scan/0b3c6e3e-0000-4000-8000-000000000000/",
                    "/scan/0b3c6e3e-0000-4000-8000-000000000000/review/"):
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.content, b"<!doctype html><div id=root></div>")
                self.assertTrue(resp["Content-Type"].startswith("text/html"))
                self.assertIn("csrftoken", resp.cookies)

    def test_503_when_the_build_is_missing(self):
        resp = self.client.get("/scan/")
        self.assertEqual(resp.status_code, 503)
        self.assertIn(b"Scan app not built: set SCAN_APP_DIR (see README)", resp.content)

    def test_spa_needs_login(self):
        self.client.logout()
        resp = self.client.get("/scan/")
        self.assertRedirects(resp, f"{reverse('login')}?next=/scan/", fetch_redirect_response=False)

    def test_collection_links_to_scan(self):
        self.assertContains(self.client.get(reverse("ledger:collection")), reverse("ledger:scan_start"))
        self.assertEqual(reverse("ledger:scan_start"), "/scan/")
