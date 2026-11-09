import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.templatetags.static import static
from django.test import TestCase


class UiAppTest(TestCase):
    """Every page URL serves the React UI's index.html; /api/, /admin/ and /static/ stay Django's."""

    INDEX = b"<!doctype html><div id=root></div>"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        override = self.settings(UI_APP_DIR=self.dir)
        override.enable()
        self.addCleanup(override.disable)

    def test_page_urls_serve_index_and_set_csrf_cookie(self):
        (self.dir / "index.html").write_bytes(self.INDEX)
        for url in ("/", "/sale/?status=sold", "/cards/12/", "/account/", "/login/?next=/sale/", "/nope/",
                    "/scan/", "/scan/0b3c6e3e-0000-4000-8000-000000000000/review/"):
            with self.subTest(url=url):
                resp = self.client.get(url)  # logged out: the UI asks /api/me/ and goes to the login page
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.content, self.INDEX)
                self.assertTrue(resp["Content-Type"].startswith("text/html"))
                self.assertIn("csrftoken", resp.cookies)

    def test_503_when_the_build_is_missing(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 503)
        self.assertIn(b"UI not built: set UI_APP_DIR (see README)", resp.content)

    def test_api_and_admin_are_not_caught(self):
        (self.dir / "index.html").write_bytes(self.INDEX)
        self.assertEqual(self.client.get("/api/nope/").status_code, 404)
        self.assertEqual(self.client.get("/admin/").status_code, 302)
        self.client.force_login(get_user_model().objects.create_superuser("boss", password="x"))
        self.assertNotEqual(self.client.get("/admin/").content, self.INDEX)

    def test_docs_login_goes_to_the_ui_login(self):
        resp = self.client.get("/api/docs/")
        self.assertRedirects(resp, "/login/?next=/api/docs/", fetch_redirect_response=False)

    def test_favicon_ico_redirects_to_the_svg(self):
        self.assertRedirects(
            self.client.get("/favicon.ico"), static("ui/favicon.svg"), fetch_redirect_response=False
        )
