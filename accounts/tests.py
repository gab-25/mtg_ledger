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


CT_ACCOUNTS = {"good-token": 7, "other-good": 8}


def fake_client(token):
    def handler(request):
        token = request.headers["Authorization"].removeprefix("Bearer ")
        for prefix, user_id in CT_ACCOUNTS.items():
            if token.startswith(prefix):
                return httpx.Response(200, json={"id": 1, "name": "My shop", "user_id": user_id})
        return httpx.Response(401, json={"error_code": "unauthorized", "extra": {"message": "bad token"}})
    return CardTraderClient(token, transport=httpx.MockTransport(handler))


@mock.patch.object(services, "make_client", side_effect=fake_client)
class CardTraderTokenTest(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("gab", password="pw")
        self.friend = User.objects.create_user("ami", password="pw")
        self.client.force_login(self.user)
        self.url = reverse("accounts:profile")

    def connect(self, token, user=None):
        if user is not None:
            self.client.force_login(user)
        return self.client.post(self.url, {"action": "cardtrader", "cardtrader-token": token})

    def test_valid_token_is_saved_on_own_account(self, _):
        resp = self.connect("  good-token ")
        self.assertRedirects(resp, self.url + "#cardtrader", fetch_redirect_response=False)
        mine = services.get_cardtrader(self.user)
        self.assertEqual((mine.api_token, mine.ct_user_id), ("good-token", 7))
        self.assertEqual(services.get_cardtrader(self.friend).api_token, "")
        page = self.client.get(self.url)
        self.assertContains(page, "Replace token")
        self.assertNotContains(page, "good-token")  # never rendered back
        self.assertTrue(SyncLog.objects.filter(owner=self.user, message__contains="API token connected").exists())

    def test_long_jwt_token_is_saved(self, _):
        token = "good-token." + "x" * 1200  # CardTrader tokens are JWTs, longer than 500 characters
        resp = self.connect(token)
        self.assertRedirects(resp, self.url + "#cardtrader", fetch_redirect_response=False)
        self.assertEqual(services.get_cardtrader(self.user).api_token, token)

    def test_rejected_token_is_not_saved(self, _):
        resp = self.connect("bad-token")
        self.assertContains(resp, "CardTrader rejected this token")
        self.assertEqual(services.get_cardtrader(self.user).api_token, "")

    def test_same_cardtrader_account_rejected_for_second_user(self, _):
        self.connect("good-token")
        resp = self.connect("good-token-2", user=self.friend)
        self.assertContains(resp, "already connected to another user")
        self.assertEqual(services.get_cardtrader(self.friend).api_token, "")
        self.assertEqual(services.get_cardtrader(self.user).api_token, "good-token")

    def test_migrated_token_without_user_id_still_blocks_duplicates(self, _):
        mine = services.get_cardtrader(self.user)
        mine.api_token = "good-token"  # copied by the migration: its CardTrader user is not known yet
        mine.save()
        resp = self.connect("good-token-2", user=self.friend)
        self.assertContains(resp, "already connected to another user")
        mine.refresh_from_db()
        self.assertEqual(mine.ct_user_id, 7)

    def test_reconnecting_own_account_is_allowed(self, _):
        self.connect("good-token")
        resp = self.connect("good-token-new")
        self.assertRedirects(resp, self.url + "#cardtrader", fetch_redirect_response=False)
        self.assertEqual(services.get_cardtrader(self.user).api_token, "good-token-new")

    def test_another_account_for_the_friend_is_fine(self, _):
        self.connect("good-token")
        self.connect("other-good", user=self.friend)
        self.assertEqual(services.get_cardtrader(self.friend).ct_user_id, 8)

    def test_remove_token(self, _):
        self.connect("good-token")
        self.connect("other-good", user=self.friend)
        self.client.post(self.url, {"action": "cardtrader_remove"})
        friend = services.get_cardtrader(self.friend)
        self.assertEqual((friend.api_token, friend.ct_user_id), ("", None))
        self.assertEqual(services.get_cardtrader(self.user).api_token, "good-token")
        self.assertContains(self.client.get(self.url), "Not connected")

    def test_admin_sees_shared_token_warning(self, _):
        admin = get_user_model().objects.create_superuser("boss", password="pw")
        self.connect("good-token", user=admin)
        page = self.client.get(self.url)
        self.assertContains(page, "also used to load the card catalog and prices for every user")
        self.assertContains(page, "Catalog, prices and scanning will stop for everyone")
        self.connect("other-good", user=self.friend)
        page = self.client.get(self.url)
        self.assertNotContains(page, "also used to load the card catalog")
        self.assertNotContains(page, "will stop for everyone")

    def test_environment_token_is_not_used(self, _):
        with mock.patch.dict("os.environ", {"CARDTRADER_API_TOKEN": "env-token"}):
            resp = self.client.get(self.url)
        self.assertContains(resp, "Not connected")
        self.assertNotContains(resp, "CARDTRADER_API_TOKEN")

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
