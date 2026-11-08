from unittest import mock

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from ledger import services
from ledger.cardtrader import CardTraderClient
from ledger.models import SyncLog, SyncService


class ProfileTest(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("gab", email="gab@example.com", password="old-pass-123")
        User.objects.create_user("other", email="other@example.com", password="x")
        self.client.force_login(self.user)
        self.url = reverse("accounts:profile")

    def test_requires_login(self):
        self.client.logout()
        self.assertRedirects(self.client.get(self.url), f"{reverse('login')}?next={self.url}")

    def test_page_renders_and_sidebar_links_here(self):
        resp = self.client.get(self.url)
        self.assertContains(resp, "Personal details")
        self.assertContains(resp, f'href="{self.url}" aria-current="page"')
        self.assertContains(resp, "Not connected")

    def test_update_details(self):
        resp = self.client.post(self.url, {
            "action": "profile", "profile-first_name": "Gabriele", "profile-last_name": "S",
            "profile-email": "new@example.com",
        })
        self.assertRedirects(resp, self.url)
        self.user.refresh_from_db()
        self.assertEqual(self.user.get_full_name(), "Gabriele S")
        self.assertEqual(self.user.email, "new@example.com")

    def test_email_must_be_unique(self):
        resp = self.client.post(self.url, {"action": "profile", "profile-email": "OTHER@example.com"})
        self.assertContains(resp, "already used by another account")

    def test_change_password_keeps_session(self):
        resp = self.client.post(self.url, {
            "action": "password", "password-old_password": "old-pass-123",
            "password-new_password1": "N3w-secret-pass!", "password-new_password2": "N3w-secret-pass!",
        })
        self.assertRedirects(resp, self.url)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("N3w-secret-pass!"))
        self.assertEqual(self.client.get(self.url).status_code, 200)  # still logged in

    def test_wrong_old_password(self):
        resp = self.client.post(self.url, {
            "action": "password", "password-old_password": "nope",
            "password-new_password1": "N3w-secret-pass!", "password-new_password2": "N3w-secret-pass!",
        })
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("old-pass-123"))


def fake_client(service=None, token=None):
    def handler(request):
        if request.headers["Authorization"] == "Bearer good-token":
            return httpx.Response(200, json={"id": 1, "name": "My shop", "user_id": 7})
        return httpx.Response(401, json={"error_code": "unauthorized", "extra": {"message": "bad token"}})
    return CardTraderClient(token or services.api_token(service), transport=httpx.MockTransport(handler))


@mock.patch.object(services, "make_client", side_effect=fake_client)
class CardTraderTokenTest(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("gab", password="pw")
        self.client.force_login(self.user)
        self.url = reverse("accounts:profile")

    def connect(self, token):
        return self.client.post(self.url, {"action": "cardtrader", "cardtrader-token": token})

    def test_valid_token_is_checked_and_saved(self, _):
        resp = self.connect("  good-token ")
        self.assertRedirects(resp, self.url + "#cardtrader", fetch_redirect_response=False)
        self.assertEqual(services.get_cardtrader().api_token, "good-token")
        page = self.client.get(self.url)
        self.assertContains(page, "Replace token")
        self.assertNotContains(page, "good-token")  # never rendered back
        self.assertTrue(SyncLog.objects.filter(message__contains="API token connected").exists())

    def test_rejected_token_is_not_saved(self, _):
        resp = self.connect("bad-token")
        self.assertContains(resp, "CardTrader rejected this token")
        self.assertEqual(services.get_cardtrader().api_token, "")

    def test_remove_token(self, _):
        self.connect("good-token")
        self.client.post(self.url, {"action": "cardtrader_remove"})
        self.assertEqual(services.get_cardtrader().api_token, "")
        self.assertContains(self.client.get(self.url), "Not connected")

    @override_settings(CARDTRADER_API_TOKEN="env-token")
    def test_environment_token_is_reported(self, _):
        resp = self.client.get(self.url)
        self.assertContains(resp, "CARDTRADER_API_TOKEN")
        self.assertContains(resp, "Connected")

    def test_sync_settings_links_to_profile(self, _):
        resp = self.client.get(reverse("ledger:sync"))
        self.assertContains(resp, f'href="{self.url}#cardtrader"')


class OpenRouterNotInProfileTest(TestCase):
    """OpenRouter is configured only through OPENROUTER_API_KEY, never from the profile."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("gab", password="pw")
        self.client.force_login(self.user)
        self.url = reverse("accounts:profile")

    def test_profile_has_no_openrouter_section(self):
        resp = self.client.get(self.url)
        self.assertNotContains(resp, 'id="openrouter"')
        self.assertNotContains(resp, "openrouter-key")

    @override_settings(OPENROUTER_API_KEY="")
    def test_openrouter_key_cannot_be_saved_from_profile(self):
        self.client.post(self.url, {"action": "openrouter", "openrouter-key": "sk-or-secret"})
        self.assertEqual(services.openrouter_key(), "")
        self.assertFalse(SyncService.objects.filter(name="OpenRouter").exists())

    @override_settings(OPENROUTER_API_KEY="env-key")
    def test_openrouter_key_comes_from_environment(self):
        self.assertEqual(services.openrouter_key(), "env-key")
