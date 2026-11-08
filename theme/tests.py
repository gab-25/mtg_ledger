from django.contrib.auth import get_user_model
from django.templatetags.static import static
from django.test import TestCase
from django.urls import reverse


class FaviconTest(TestCase):
    def test_pages_link_the_svg_favicon(self):
        self.assertContains(self.client.get(reverse("login")), f'<link rel="icon" href="{static("theme/favicon.svg")}"')
        self.client.force_login(get_user_model().objects.create_user("gab", password="x"))
        self.assertContains(self.client.get(reverse("ledger:collection")), 'rel="icon"')

    def test_favicon_ico_redirects_to_the_svg(self):
        self.assertRedirects(
            self.client.get("/favicon.ico"), static("theme/favicon.svg"), fetch_redirect_response=False
        )
