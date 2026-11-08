from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


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
        self.assertContains(resp, "API token missing")

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
