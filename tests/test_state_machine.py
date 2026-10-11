from decimal import Decimal

from django.db import IntegrityError, transaction
from rest_framework.test import APIClient

from accounts.models import ProviderProfile
from consultations.models import Session

from .helpers import BillingTestCase, api_client, balance, fund, make_provider, make_user, session_row


class StateMachineTests(BillingTestCase):
    def _new(self):
        return self.user_api.post("/api/sessions/", format="json").json()["id"]

    def _assign(self, sid):
        return self.user_api.post(f"/api/sessions/{sid}/assign/", {"provider_id": self.provider.id}, format="json")

    # --- forward path --------------------------------------------------------------------
    def test_full_lifecycle_statuses(self):
        fund(self.user, "500.00")
        sid = self._new()
        self.assertEqual(session_row(sid).status, "unassigned")
        self._assign(sid)
        self.assertEqual(session_row(sid).status, "assigned")
        self.provider_api.post(f"/api/sessions/{sid}/accept/", format="json")
        self.assertEqual(session_row(sid).status, "accepted_by_provider")
        self.user_api.post(f"/api/sessions/{sid}/start/", format="json")
        self.assertEqual(session_row(sid).status, "in_progress")
        self.user_api.post(f"/api/sessions/{sid}/end/", format="json")
        self.assertEqual(session_row(sid).status, "completed")

    def test_provider_can_end_the_session(self):
        sid = self.in_progress_session()
        self.clock.advance(30)
        r = self.provider_api.post(f"/api/sessions/{sid}/end/", format="json")
        self.assertEqual(r.status_code, 200)
        s = session_row(sid)
        self.assertEqual((s.status, s.ended_by, s.talk_seconds), ("completed", "provider", 30))
        self.assertEqual(balance(self.user), Decimal("450.00"))  # only minute 1 charged

    # --- invalid transitions: every wrong action in every state is a 400 ------------------
    def test_invalid_transition_matrix(self):
        fund(self.user, "500.00")

        def check(sid, api, action, body=None):
            r = api.post(f"/api/sessions/{sid}/{action}/", body, format="json")
            self.assertEqual(r.status_code, 400, f"{action} -> {r.status_code} {r.content}")
            self.assertEqual(r.json()["code"], "invalid_transition")

        unassigned = self._new()
        for action in ("start", "end", "bill", ):
            with self.subTest(state="unassigned", action=action):
                check(unassigned, self.user_api, action)

        assigned = self._new()
        self._assign(assigned)
        for api, action, body in [
            (self.user_api, "start", None),
            (self.user_api, "end", None),
            (self.user_api, "bill", None),
            (self.user_api, "assign", {"provider_id": self.provider.id}),
        ]:
            with self.subTest(state="assigned", action=action):
                check(assigned, api, action, body)
        with self.subTest(state="assigned", action="provider end"):
            check(assigned, self.provider_api, "end")

        accepted = self.accepted_session()
        for api, action, body in [
            (self.user_api, "end", None),
            (self.user_api, "bill", None),
            (self.provider_api, "accept", None),
            (self.user_api, "assign", {"provider_id": self.provider.id}),
        ]:
            with self.subTest(state="accepted", action=action):
                check(accepted, api, action, body)

        done = self.accepted_session()
        self.user_api.post(f"/api/sessions/{done}/start/", format="json")
        self.user_api.post(f"/api/sessions/{done}/end/", format="json")
        for api, action, body in [
            (self.user_api, "start", None),
            (self.user_api, "end", None),
            (self.provider_api, "end", None),
            (self.user_api, "bill", None),
            (self.user_api, "cancel", None),
            (self.provider_api, "accept", None),
        ]:
            with self.subTest(state="completed", action=action):
                check(done, api, action, body)

    def test_cancel_before_start_then_nothing_else_works(self):
        sid = self.accepted_session()
        r = self.user_api.post(f"/api/sessions/{sid}/cancel/", format="json")
        self.assertEqual(r.status_code, 200)
        s = session_row(sid)
        self.assertEqual((s.status, s.ended_by), ("cancelled", "user"))
        r = self.user_api.post(f"/api/sessions/{sid}/start/", format="json")
        self.assertEqual(r.status_code, 400)

    def test_cannot_cancel_once_in_progress(self):
        sid = self.in_progress_session()
        r = self.user_api.post(f"/api/sessions/{sid}/cancel/", format="json")
        self.assertEqual(r.status_code, 400)

    # --- who may do what -----------------------------------------------------------------
    def test_roles_and_ownership_are_enforced(self):
        sid = self._new()
        self._assign(sid)
        other_user = api_client(make_user("mallory"))
        other_provider = api_client(make_provider("other-astro"))

        self.assertEqual(self.user_api.post(f"/api/sessions/{sid}/accept/", format="json").status_code, 403)
        self.assertEqual(other_provider.post(f"/api/sessions/{sid}/accept/", format="json").status_code, 403)
        self.assertEqual(self.provider_api.post("/api/sessions/", format="json").status_code, 403)
        self.assertEqual(other_user.post(f"/api/sessions/{sid}/assign/", {"provider_id": self.provider.id}, format="json").status_code, 403)

        self.provider_api.post(f"/api/sessions/{sid}/accept/", format="json")
        fund(self.user, "500.00")
        self.assertEqual(other_user.post(f"/api/sessions/{sid}/start/", format="json").status_code, 403)
        self.assertEqual(self.provider_api.post(f"/api/sessions/{sid}/start/", format="json").status_code, 403)

        self.user_api.post(f"/api/sessions/{sid}/start/", format="json")
        self.assertEqual(other_user.post(f"/api/sessions/{sid}/end/", format="json").status_code, 403)
        self.assertEqual(other_user.post(f"/api/sessions/{sid}/bill/", format="json").status_code, 403)
        self.assertEqual(other_provider.post(f"/api/sessions/{sid}/end/", format="json").status_code, 403)
        self.assertEqual(session_row(sid).status, "in_progress")

    def test_sessions_are_invisible_to_outsiders(self):
        sid = self._new()
        stranger = api_client(make_user("stranger"))
        self.assertEqual(stranger.get(f"/api/sessions/{sid}/").status_code, 404)
        self.assertEqual(stranger.get("/api/sessions/").json(), [])
        self.assertEqual(self.user_api.get(f"/api/sessions/{sid}/").status_code, 200)

    def test_unauthenticated_requests_are_rejected(self):
        self.assertEqual(APIClient().post("/api/sessions/", format="json").status_code, 401)

    def test_cannot_assign_to_a_non_provider(self):
        sid = self._new()
        r = self.user_api.post(f"/api/sessions/{sid}/assign/", {"provider_id": self.user.id}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(session_row(sid).status, "unassigned")

    def test_unknown_session_is_404(self):
        self.assertEqual(self.user_api.post("/api/sessions/9999/start/", format="json").status_code, 404)

    # --- one active session per user -----------------------------------------------------
    def test_user_cannot_have_two_in_progress_sessions(self):
        first = self.in_progress_session(funds="1000.00")
        second = self.accepted_session()

        r = self.user_api.post(f"/api/sessions/{second}/start/", format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "active_session_exists")
        self.assertEqual(session_row(second).status, "accepted_by_provider")

        self.user_api.post(f"/api/sessions/{first}/end/", format="json")
        r = self.user_api.post(f"/api/sessions/{second}/start/", format="json")
        self.assertEqual(r.status_code, 200)

    def test_database_enforces_one_active_session_even_if_the_code_is_bypassed(self):
        a = self.accepted_session()
        b = self.accepted_session()
        Session.objects.filter(pk=a).update(status="in_progress")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Session.objects.filter(pk=b).update(status="in_progress")

    # --- rate is frozen ------------------------------------------------------------------
    def test_per_minute_cost_is_frozen_when_the_session_starts(self):
        sid = self.in_progress_session()
        self.assertEqual(session_row(sid).per_minute_cost, Decimal("50.00"))

        ProviderProfile.objects.filter(user=self.provider).update(per_minute_rate=Decimal("200.00"))
        self.clock.advance(61)
        self.bill(sid)

        self.assertEqual(session_row(sid).per_minute_cost, Decimal("50.00"))
        self.assertEqual(balance(self.user), Decimal("400.00"))  # 500 - 2 x 50, not 500 - 50 - 200
