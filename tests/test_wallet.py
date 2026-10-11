import uuid
from decimal import Decimal

from django.db import IntegrityError, transaction
from rest_framework.test import APIClient, APITestCase

from common.exceptions import InsufficientBalance
from wallet import services
from wallet.models import LedgerEntry, RechargeTransaction, Wallet

from .helpers import WEBHOOK, api_client, balance, fund, make_user


class WalletTests(APITestCase):
    def setUp(self):
        self.user = make_user()
        self.api = api_client(self.user)
        self.gateway = APIClient()

    def _initiate(self, amount="200.00"):
        r = self.api.post("/api/wallet/recharge/", {"amount": amount}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["reference"]

    def _callback(self, reference, status="success", headers=WEBHOOK, **extra):
        return self.gateway.post(
            "/api/wallet/recharge/callback/", {"reference": reference, "status": status, **extra}, format="json", **headers
        )

    # --- viewing -------------------------------------------------------------------------
    def test_view_balance(self):
        fund(self.user, "123.45")
        r = self.api.get("/api/wallet/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Decimal(r.json()["balance"]), Decimal("123.45"))

    def test_wallet_requires_authentication(self):
        self.assertEqual(APIClient().get("/api/wallet/").status_code, 401)

    # --- recharge ------------------------------------------------------------------------
    def test_initiating_a_recharge_does_not_touch_the_balance(self):
        self._initiate("200.00")
        txn = RechargeTransaction.objects.get()
        self.assertEqual(txn.status, RechargeTransaction.Status.PENDING)
        self.assertEqual(balance(self.user), Decimal("0.00"))

    def test_rejects_non_positive_or_garbage_amounts(self):
        for bad in ["0", "-5", "abc", "10.999", ""]:
            with self.subTest(amount=bad):
                r = self.api.post("/api/wallet/recharge/", {"amount": bad}, format="json")
                self.assertEqual(r.status_code, 400)
        self.assertEqual(RechargeTransaction.objects.count(), 0)

    def test_success_callback_credits_wallet_and_writes_ledger(self):
        ref = self._initiate("200.00")
        r = self._callback(ref)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "success")
        self.assertEqual(balance(self.user), Decimal("200.00"))
        entry = LedgerEntry.objects.get()
        self.assertEqual((entry.amount, entry.balance_after), (Decimal("200.00"), Decimal("200.00")))

    def test_callback_amount_in_payload_is_ignored(self):
        ref = self._initiate("200.00")
        self._callback(ref, amount="999999.00")
        self.assertEqual(balance(self.user), Decimal("200.00"))

    def test_callback_requires_the_shared_secret(self):
        ref = self._initiate()
        for headers in [{}, {"HTTP_X_WEBHOOK_SECRET": "wrong"}]:
            with self.subTest(headers=headers):
                self.assertEqual(self._callback(ref, headers=headers).status_code, 403)
        self.assertEqual(balance(self.user), Decimal("0.00"))
        self.assertEqual(RechargeTransaction.objects.get().status, RechargeTransaction.Status.PENDING)

    def test_unknown_reference_is_404(self):
        self.assertEqual(self._callback("rch_does_not_exist").status_code, 404)

    def test_failed_callback_never_credits(self):
        ref = self._initiate()
        r = self._callback(ref, "failed")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["credited"])
        self.assertEqual(balance(self.user), Decimal("0.00"))
        self.assertEqual(RechargeTransaction.objects.get().status, RechargeTransaction.Status.FAILED)

    def test_replayed_failed_callback_is_a_noop(self):
        ref = self._initiate()
        self._callback(ref, "failed")
        r = self._callback(ref, "failed")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["credited"])

    def test_conflicting_callbacks_are_rejected_with_409(self):
        paid = self._initiate()
        self._callback(paid, "success")
        r = self._callback(paid, "failed")  # cannot un-pay
        self.assertEqual(r.status_code, 409)
        self.assertEqual(balance(self.user), Decimal("200.00"))

        failed = self._initiate()
        self._callback(failed, "failed")
        r = self._callback(failed, "success")  # a failed txn cannot be revived
        self.assertEqual(r.status_code, 409)
        self.assertEqual(balance(self.user), Decimal("200.00"))

    def test_callback_rejects_unknown_status(self):
        ref = self._initiate()
        self.assertEqual(self._callback(ref, "pending").status_code, 400)

    # --- balance guarantees --------------------------------------------------------------
    def test_debit_beyond_balance_is_refused_and_leaves_wallet_untouched(self):
        fund(self.user, "40.00")
        with transaction.atomic():
            wallet = services.lock_wallet(self.user)
            with self.assertRaises(InsufficientBalance):
                services.debit(wallet, Decimal("50.00"), kind=LedgerEntry.Kind.SESSION_DEBIT, reference="x:1")
        self.assertEqual(balance(self.user), Decimal("40.00"))
        self.assertFalse(LedgerEntry.objects.filter(reference="x:1").exists())

    def test_debit_of_exact_balance_is_allowed_and_leaves_zero(self):
        fund(self.user, "50.00")
        with transaction.atomic():
            wallet = services.lock_wallet(self.user)
            services.debit(wallet, Decimal("50.00"), kind=LedgerEntry.Kind.SESSION_DEBIT, reference="x:2")
        self.assertEqual(balance(self.user), Decimal("0.00"))

    def test_database_itself_refuses_a_negative_balance(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Wallet.objects.filter(user=self.user).update(balance=Decimal("-0.01"))

    def test_ledger_reference_is_unique(self):
        with transaction.atomic():
            wallet = services.lock_wallet(self.user)
            services.credit(wallet, Decimal("10"), kind=LedgerEntry.Kind.RECHARGE, reference="dup")
        with self.assertRaises(IntegrityError), transaction.atomic():
            wallet = services.lock_wallet(self.user)
            services.credit(wallet, Decimal("10"), kind=LedgerEntry.Kind.RECHARGE, reference="dup")
        self.assertEqual(balance(self.user), Decimal("10.00"))
