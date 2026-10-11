"""The six scenarios the assignment requires, numbered exactly as in the brief."""
from decimal import Decimal

from rest_framework.test import APIClient

from consultations.models import Session, SessionBilling
from wallet.models import LedgerEntry

from .helpers import WEBHOOK, BillingTestCase, balance, fund, session_row


class RequiredScenarios(BillingTestCase):
    # 1 ---------------------------------------------------------------------------------
    def test_1_insufficient_balance_cannot_start(self):
        """Rate is 50, so 5 x 50 = 250 is needed. 249.99 must not be enough."""
        fund(self.user, "249.99")
        sid = self.accepted_session()

        r = self.user_api.post(f"/api/sessions/{sid}/start/", format="json")

        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "insufficient_balance")
        self.assertEqual(session_row(sid).status, Session.Status.ACCEPTED_BY_PROVIDER)
        self.assertEqual(balance(self.user), Decimal("249.99"))  # nothing was charged

    # 2 ---------------------------------------------------------------------------------
    def test_2_happy_path(self):
        """Recharge -> create -> accept -> start -> 2 minutes billed -> end. Wallet debited correctly."""
        # Recharge through the real two-step flow (initiate, then gateway callback).
        r = self.user_api.post("/api/wallet/recharge/", {"amount": "500.00"}, format="json")
        self.assertEqual(r.status_code, 201)
        reference = r.json()["reference"]
        r = APIClient().post(
            "/api/wallet/recharge/callback/", {"reference": reference, "status": "success"}, format="json", **WEBHOOK
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(balance(self.user), Decimal("500.00"))

        sid = self.accepted_session()
        r = self.user_api.post(f"/api/sessions/{sid}/start/", format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "in_progress")
        self.assertEqual(balance(self.user), Decimal("450.00"))  # minute 1 charged at start

        self.clock.advance(90)  # 1m30s in -> minute 2 has begun
        body = self.bill(sid)
        self.assertEqual(body["minutes_billed_now"], 1)
        self.assertEqual(balance(self.user), Decimal("400.00"))

        self.clock.advance(10)  # 1m40s: still inside minute 2, nothing more to charge
        r = self.user_api.post(f"/api/sessions/{sid}/end/", format="json")
        self.assertEqual(r.status_code, 200)

        s = session_row(sid)
        self.assertEqual(s.status, Session.Status.COMPLETED)
        self.assertEqual(s.ended_by, Session.EndedBy.USER)
        self.assertEqual(s.billed_minutes, 2)
        self.assertEqual(s.billed_amount, Decimal("100.00"))
        self.assertEqual(s.talk_seconds, 100)
        self.assertEqual(balance(self.user), Decimal("400.00"))  # 500 - 2 x 50
        debits = LedgerEntry.objects.filter(kind=LedgerEntry.Kind.SESSION_DEBIT)
        self.assertEqual(debits.count(), 2)

    # 3 ---------------------------------------------------------------------------------
    def test_3_balance_runs_out_mid_session(self):
        """Wallet 290, rate 50: five minutes are affordable (250), leaving 40 which cannot pay minute 6."""
        sid = self.in_progress_session(funds="290.00")  # minute 1 paid at start -> 240
        t0 = session_row(sid).started_at

        for expected_balance in ["190.00", "140.00", "90.00", "40.00"]:
            self.clock.advance(61)
            self.bill(sid)
            self.assertEqual(balance(self.user), Decimal(expected_balance))
            self.assertEqual(session_row(sid).status, Session.Status.IN_PROGRESS)

        self.clock.advance(61)  # elapsed 305s: minute 6 is due, wallet holds 40 < 50
        self.bill(sid)

        s = session_row(sid)
        self.assertEqual(s.status, Session.Status.COMPLETED)
        self.assertEqual(s.ended_by, Session.EndedBy.SYSTEM)
        self.assertEqual(s.end_reason, Session.EndReason.INSUFFICIENT_BALANCE)
        self.assertEqual(s.billed_minutes, 5)
        self.assertEqual(s.billed_amount, Decimal("250.00"))
        self.assertEqual(s.talk_seconds, 300)  # persisted: only what was paid for
        self.assertEqual((s.ended_at - t0).total_seconds(), 300)
        self.assertEqual(balance(self.user), Decimal("40.00"))  # not negative, not partially debited

        # A further tick must not touch the wallet.
        r = self.user_api.post(f"/api/sessions/{sid}/bill/", format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(balance(self.user), Decimal("40.00"))

    # 4 ---------------------------------------------------------------------------------
    def test_4_invalid_transition_returns_400(self):
        """Ending a session before it has started is rejected with a meaningful error."""
        sid = self.accepted_session()  # accepted, but never started

        r = self.user_api.post(f"/api/sessions/{sid}/end/", format="json")

        self.assertEqual(r.status_code, 400)
        body = r.json()
        self.assertEqual(body["code"], "invalid_transition")
        self.assertEqual(body["current_status"], "accepted_by_provider")
        self.assertIn("Cannot end", body["error"])
        self.assertEqual(session_row(sid).status, Session.Status.ACCEPTED_BY_PROVIDER)

    # 5 ---------------------------------------------------------------------------------
    def test_5_duplicate_recharge_callback_credits_once(self):
        r = self.user_api.post("/api/wallet/recharge/", {"amount": "300.00"}, format="json")
        reference = r.json()["reference"]
        gateway = APIClient()
        payload = {"reference": reference, "status": "success"}

        first = gateway.post("/api/wallet/recharge/callback/", payload, format="json", **WEBHOOK)
        second = gateway.post("/api/wallet/recharge/callback/", payload, format="json", **WEBHOOK)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(first.json()["credited"])
        self.assertFalse(second.json()["credited"])
        self.assertEqual(balance(self.user), Decimal("300.00"))  # once, not 600
        self.assertEqual(LedgerEntry.objects.filter(kind=LedgerEntry.Kind.RECHARGE).count(), 1)

    # 6 ---------------------------------------------------------------------------------
    def test_6_duplicate_billing_does_not_double_charge(self):
        sid = self.in_progress_session(funds="500.00")  # minute 1 charged -> 450
        self.clock.advance(30)  # still inside minute 1

        for _ in range(3):
            body = self.bill(sid)
            self.assertEqual(body["minutes_billed_now"], 0)
        self.assertEqual(balance(self.user), Decimal("450.00"))

        self.clock.advance(31)  # elapsed 61s: minute 2 is now billable, exactly once
        first = self.bill(sid)
        second = self.bill(sid)

        self.assertEqual(first["minutes_billed_now"], 1)
        self.assertEqual(second["minutes_billed_now"], 0)
        self.assertEqual(balance(self.user), Decimal("400.00"))
        self.assertEqual(SessionBilling.objects.filter(session_id=sid).count(), 2)
        self.assertEqual(LedgerEntry.objects.filter(kind=LedgerEntry.Kind.SESSION_DEBIT).count(), 2)
