"""Real parallel-request tests. They need a database with row locks, so they run on PostgreSQL
and are skipped on SQLite (which has no SELECT ... FOR UPDATE)."""
import threading
from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.db import close_old_connections, connections
from django.test import TransactionTestCase, skipUnlessDBFeature

from common.exceptions import ActiveSessionExists
from consultations import services
from consultations.models import Session, SessionBilling
from wallet.models import LedgerEntry, RechargeTransaction, Wallet
from wallet import services as wallet_services

from .helpers import Clock, balance, fund, make_provider, make_user

THREADS = 8


def run_in_threads(fn):
    errors, results = [], []

    def worker():
        close_old_connections()
        try:
            results.append(fn())
        except Exception as exc:  # noqa: BLE001 - collected and inspected by the test
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


@skipUnlessDBFeature("has_select_for_update")
class ConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.clock = Clock()
        patcher = mock.patch("django.utils.timezone.now", side_effect=lambda: self.clock.now())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = make_user()
        self.provider = make_provider(rate="50.00")

    def _accepted(self):
        s = services.create_session(self.user)
        services.assign_session(s.pk, self.user, self.provider.pk)
        services.accept_session(s.pk, self.provider)
        return s.pk

    def test_parallel_billing_ticks_charge_each_minute_once(self):
        fund(self.user, "1000.00")
        sid = self._accepted()
        services.start_session(sid, self.user)  # minute 1
        self.clock.advance(90)  # minute 2 is due

        results, errors = run_in_threads(lambda: services.bill_session(sid, now=self.clock.now()))

        self.assertEqual(errors, [])
        self.assertEqual(sum(r.minutes_billed for r in results), 1)  # exactly one thread billed it
        self.assertEqual(Session.objects.get(pk=sid).billed_minutes, 2)
        self.assertEqual(SessionBilling.objects.filter(session_id=sid).count(), 2)
        self.assertEqual(LedgerEntry.objects.filter(kind="session_debit").count(), 2)
        self.assertEqual(balance(self.user), Decimal("900.00"))

    def test_parallel_duplicate_callbacks_credit_once(self):
        txn = wallet_services.initiate_recharge(self.user, Decimal("300.00"))

        results, errors = run_in_threads(
            lambda: wallet_services.process_recharge_callback(reference=txn.reference, status="success")
        )

        self.assertEqual(errors, [])
        self.assertEqual(sum(1 for _, credited in results if credited), 1)
        self.assertEqual(balance(self.user), Decimal("300.00"))
        self.assertEqual(LedgerEntry.objects.filter(kind="recharge").count(), 1)

    def test_parallel_starts_for_one_user_allow_exactly_one(self):
        fund(self.user, "1000.00")
        ids = [self._accepted() for _ in range(THREADS)]
        it = iter(ids)
        lock = threading.Lock()

        def start_next():
            with lock:
                sid = next(it)
            return services.start_session(sid, self.user, now=self.clock.now())

        results, errors = run_in_threads(start_next)

        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), THREADS - 1)
        self.assertTrue(all(isinstance(e, ActiveSessionExists) for e in errors))
        self.assertEqual(Session.objects.filter(status="in_progress").count(), 1)
        self.assertEqual(balance(self.user), Decimal("950.00"))  # only one first-minute charge
