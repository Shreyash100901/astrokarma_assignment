import uuid
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.db import transaction
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient, APITestCase

from accounts.models import ProviderProfile, User
from consultations.models import Session
from wallet import services as wallet_services
from wallet.models import LedgerEntry, Wallet

WEBHOOK = {"HTTP_X_WEBHOOK_SECRET": settings.MOCK_WEBHOOK_SECRET}


class Clock:
    """Controllable replacement for timezone.now(). Tests never sleep."""

    def __init__(self):
        self.current = datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt_timezone.utc)

    def now(self):
        return self.current

    def advance(self, seconds):
        self.current += timedelta(seconds=seconds)
        return self.current


def make_user(username="alice"):
    user = User.objects.create_user(username, password="pw-12345678", role=User.Role.USER)
    Wallet.objects.create(user=user)
    return user


def make_provider(username="astro", rate="50.00"):
    user = User.objects.create_user(username, password="pw-12345678", role=User.Role.PROVIDER)
    ProviderProfile.objects.create(user=user, per_minute_rate=Decimal(rate))
    Wallet.objects.create(user=user)
    return user


def api_client(user):
    client = APIClient()
    token, _ = Token.objects.get_or_create(user=user)
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


def fund(user, amount):
    """Put money in a wallet directly (bypasses the recharge flow, for test setup only)."""
    with transaction.atomic():
        wallet = wallet_services.lock_wallet(user)
        wallet_services.credit(
            wallet, Decimal(amount), kind=LedgerEntry.Kind.RECHARGE, reference=f"test-fund:{uuid.uuid4().hex}"
        )


def balance(user):
    return Wallet.objects.get(user=user).balance


def session_row(session_id):
    return Session.objects.get(pk=session_id)


class BillingTestCase(APITestCase):
    """Fixture: one user, one provider charging RATE/min, and a controllable clock."""

    RATE = "50.00"

    def setUp(self):
        self.clock = Clock()
        patcher = mock.patch("django.utils.timezone.now", side_effect=lambda: self.clock.now())
        patcher.start()
        self.addCleanup(patcher.stop)

        self.user = make_user()
        self.provider = make_provider(rate=self.RATE)
        self.user_api = api_client(self.user)
        self.provider_api = api_client(self.provider)

    def accepted_session(self, user_api=None):
        """Create -> assign -> accept. Returns the session id."""
        user_api = user_api or self.user_api
        created = user_api.post("/api/sessions/", format="json")
        self.assertEqual(created.status_code, 201, created.content)
        sid = created.json()["id"]
        r = user_api.post(f"/api/sessions/{sid}/assign/", {"provider_id": self.provider.id}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        r = self.provider_api.post(f"/api/sessions/{sid}/accept/", format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return sid

    def in_progress_session(self, funds="500.00"):
        """Fund the wallet and start a session at the current clock time. Returns the session id."""
        fund(self.user, funds)
        sid = self.accepted_session()
        r = self.user_api.post(f"/api/sessions/{sid}/start/", format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return sid

    def bill(self, sid, api=None):
        r = (api or self.user_api).post(f"/api/sessions/{sid}/bill/", format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()
