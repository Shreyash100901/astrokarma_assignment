from decimal import Decimal
from io import StringIO

from django.core.management import call_command

from consultations.models import Session

from .helpers import BillingTestCase, balance, fund, session_row


class BillingTests(BillingTestCase):
    def test_start_charges_the_first_minute_immediately(self):
        sid = self.in_progress_session("500.00")
        s = session_row(sid)
        self.assertEqual((s.billed_minutes, s.billed_amount), (1, Decimal("50.00")))
        self.assertEqual(balance(self.user), Decimal("450.00"))

    def test_balance_of_exactly_five_minutes_is_enough_to_start(self):
        sid = self.in_progress_session("250.00")
        self.assertEqual(session_row(sid).status, "in_progress")
        self.assertEqual(balance(self.user), Decimal("200.00"))

    def test_spec_example_three_minutes_costs_150(self):
        """From the brief: rate 50, duration 3 minutes -> total 150."""
        sid = self.in_progress_session("500.00")
        self.clock.advance(180)
        self.user_api.post(f"/api/sessions/{sid}/end/", format="json")
        s = session_row(sid)
        self.assertEqual((s.billed_minutes, s.billed_amount), (3, Decimal("150.00")))
        self.assertEqual(balance(self.user), Decimal("350.00"))

    def test_a_started_minute_is_charged_in_full(self):
        sid = self.in_progress_session("500.00")
        self.clock.advance(61)  # one second into minute 2
        self.user_api.post(f"/api/sessions/{sid}/end/", format="json")
        self.assertEqual(session_row(sid).billed_minutes, 2)
        self.assertEqual(balance(self.user), Decimal("400.00"))

    def test_ending_instantly_costs_one_minute(self):
        sid = self.in_progress_session("500.00")
        self.user_api.post(f"/api/sessions/{sid}/end/", format="json")
        self.assertEqual(balance(self.user), Decimal("450.00"))

    def test_late_tick_catches_up_every_missed_minute(self):
        sid = self.in_progress_session("1000.00")
        self.clock.advance(305)  # nobody billed for 5 minutes
        body = self.bill(sid)
        self.assertEqual(body["minutes_billed_now"], 5)  # minutes 2..6
        self.assertEqual(body["billed_minutes"], 6)
        self.assertEqual(balance(self.user), Decimal("700.00"))
        self.assertEqual(session_row(sid).billings.count(), 6)

    def test_one_very_late_tick_ends_session_at_the_paid_boundary(self):
        sid = self.in_progress_session("290.00")
        t0 = session_row(sid).started_at
        self.clock.advance(3600)  # an hour with no billing tick
        self.bill(sid)

        s = session_row(sid)
        self.assertEqual((s.status, s.ended_by, s.end_reason), ("completed", "system", "insufficient_balance"))
        self.assertEqual(s.billed_minutes, 5)
        self.assertEqual(s.talk_seconds, 300)  # capped at paid time, not the 3600s wall clock
        self.assertEqual((s.ended_at - t0).total_seconds(), 300)
        self.assertEqual(balance(self.user), Decimal("40.00"))

    def test_wallet_can_be_drained_to_exactly_zero(self):
        sid = self.in_progress_session("250.00")
        self.clock.advance(241)  # minute 5 due: 200 -> ... -> 0
        self.bill(sid)
        self.assertEqual(balance(self.user), Decimal("0.00"))
        self.assertEqual(session_row(sid).status, "in_progress")  # all five minutes were paid
        self.clock.advance(60)  # minute 6 due, wallet is 0
        self.bill(sid)
        self.assertEqual(session_row(sid).ended_by, "system")
        self.assertEqual(balance(self.user), Decimal("0.00"))

    def test_user_ending_after_an_unpaid_minute_began_is_recorded_as_system_end(self):
        sid = self.in_progress_session("250.00")
        self.clock.advance(301)  # minute 6 has begun and cannot be paid
        r = self.user_api.post(f"/api/sessions/{sid}/end/", format="json")
        self.assertEqual(r.status_code, 200)
        s = session_row(sid)
        self.assertEqual((s.ended_by, s.end_reason, s.talk_seconds), ("system", "insufficient_balance", 300))
        self.assertEqual(balance(self.user), Decimal("0.00"))

    def test_topping_up_mid_session_keeps_the_session_alive(self):
        sid = self.in_progress_session("250.00")
        self.clock.advance(241)
        self.bill(sid)  # wallet now 0
        fund(self.user, "100.00")
        self.clock.advance(60)
        self.bill(sid)
        self.assertEqual(session_row(sid).status, "in_progress")
        self.assertEqual(balance(self.user), Decimal("50.00"))

    def test_bill_endpoint_reports_what_it_charged(self):
        sid = self.in_progress_session("500.00")
        self.clock.advance(61)
        body = self.bill(sid)
        self.assertEqual(body["minutes_billed_now"], 1)
        self.assertEqual(Decimal(body["amount_charged_now"]), Decimal("50.00"))

    def test_management_command_bills_and_auto_ends(self):
        sid = self.in_progress_session("290.00")
        self.clock.advance(400)
        out = StringIO()
        call_command("bill_active_sessions", stdout=out)
        self.assertIn("checked=1", out.getvalue())
        self.assertIn("auto_ended=1", out.getvalue())
        s = session_row(sid)
        self.assertEqual((s.status, s.ended_by), (Session.Status.COMPLETED, "system"))
        self.assertEqual(balance(self.user), Decimal("40.00"))

        out = StringIO()
        call_command("bill_active_sessions", stdout=out)  # nothing left to do, nothing re-charged
        self.assertIn("checked=0", out.getvalue())
        self.assertEqual(balance(self.user), Decimal("40.00"))
