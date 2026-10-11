from decimal import Decimal

from rest_framework.test import APIClient, APITestCase

from accounts.models import ProviderProfile, User
from wallet.models import Wallet


class AccountsTests(APITestCase):
    def setUp(self):
        self.api = APIClient()

    def test_register_user_creates_wallet_and_token(self):
        r = self.api.post("/api/auth/register/", {"username": "u1", "password": "pw-12345678", "role": "USER"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertTrue(r.json()["token"])
        self.assertEqual(Wallet.objects.get(user__username="u1").balance, Decimal("0.00"))

    def test_register_provider_requires_a_rate(self):
        body = {"username": "p1", "password": "pw-12345678", "role": "PROVIDER"}
        self.assertEqual(self.api.post("/api/auth/register/", body, format="json").status_code, 400)
        r = self.api.post("/api/auth/register/", {**body, "per_minute_rate": "75.00"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(ProviderProfile.objects.get(user__username="p1").per_minute_rate, Decimal("75.00"))

    def test_user_cannot_register_with_a_rate(self):
        body = {"username": "u2", "password": "pw-12345678", "role": "USER", "per_minute_rate": "10"}
        self.assertEqual(self.api.post("/api/auth/register/", body, format="json").status_code, 400)

    def test_duplicate_username_rejected(self):
        body = {"username": "dup", "password": "pw-12345678", "role": "USER"}
        self.api.post("/api/auth/register/", body, format="json")
        self.assertEqual(self.api.post("/api/auth/register/", {**body, "username": "DUP"}, format="json").status_code, 400)

    def test_token_login_then_providers_listing(self):
        self.api.post("/api/auth/register/", {"username": "p2", "password": "pw-12345678", "role": "PROVIDER", "per_minute_rate": "40"}, format="json")
        self.api.post("/api/auth/register/", {"username": "u3", "password": "pw-12345678", "role": "USER"}, format="json")
        token = self.api.post("/api/auth/token/", {"username": "u3", "password": "pw-12345678"}, format="json").json()["token"]
        self.api.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        r = self.api.get("/api/auth/providers/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([(p["username"], p["per_minute_rate"]) for p in r.json()], [("p2", "40.00")])
        self.assertEqual(User.objects.get(username="u3").role, "USER")
