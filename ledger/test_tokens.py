"""The shared (service) CardTrader token is the first superuser's own token."""

from django.contrib.auth import get_user_model
from django.test import TestCase

from . import services


class ServiceTokenTest(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin1 = User.objects.create_superuser("admin1", password="pw")
        self.admin2 = User.objects.create_superuser("admin2", password="pw")
        self.friend = User.objects.create_user("friend", password="pw")

    def give(self, user, token):
        service = services.get_cardtrader(user)
        service.api_token = token
        service.save()

    def test_first_superuser_with_a_token(self):
        self.give(self.admin2, "s2")
        self.give(self.friend, "u")
        self.assertEqual(services.service_token(), "s2")
        self.assertEqual(services.service_account_user(), self.admin2)
        self.give(self.admin1, "s1")
        self.assertEqual(services.service_token(), "s1")

    def test_no_superuser_token(self):
        self.give(self.friend, "u")
        self.assertEqual(services.service_token(), "")
        self.assertIsNone(services.service_account_user())

    def test_user_token_is_the_users_own(self):
        self.give(self.friend, "u")
        self.assertEqual(services.user_token(self.friend), "u")
        self.assertEqual(services.user_token(self.admin1), "")
