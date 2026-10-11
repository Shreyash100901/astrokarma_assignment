"""Every wallet mutation goes through this module.

Rules enforced here:
  * debit() is a single conditional UPDATE (`WHERE balance >= amount`), so a balance can
    never go negative even if two callers race, and even on a database without row locks.
  * every credit/debit writes a LedgerEntry whose unique `reference` is an idempotency key.
  * callers must already be inside transaction.atomic() (enforced).
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import F
from django.utils import timezone
from rest_framework.exceptions import NotFound

from common.exceptions import Conflict, InsufficientBalance

from .models import LedgerEntry, RechargeTransaction, Wallet


def _require_atomic():
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Wallet mutations must run inside transaction.atomic().")


def _money(amount) -> Decimal:
    amount = Decimal(amount).quantize(Decimal("0.01"))
    if amount <= 0:
        raise ValueError("Amount must be positive.")
    return amount


def lock_wallet(user) -> Wallet:
    """Fetch the user's wallet row with a row lock. Also acts as a per-user mutex."""
    _require_atomic()
    Wallet.objects.get_or_create(user=user)
    return Wallet.objects.select_for_update().get(user=user)


def credit(wallet: Wallet, amount, *, kind, reference) -> LedgerEntry:
    _require_atomic()
    amount = _money(amount)
    Wallet.objects.filter(pk=wallet.pk).update(balance=F("balance") + amount, updated_at=timezone.now())
    wallet.refresh_from_db(fields=["balance", "updated_at"])
    return LedgerEntry.objects.create(
        wallet=wallet, amount=amount, kind=kind, reference=reference, balance_after=wallet.balance
    )


def debit(wallet: Wallet, amount, *, kind, reference) -> LedgerEntry:
    _require_atomic()
    amount = _money(amount)
    updated = Wallet.objects.filter(pk=wallet.pk, balance__gte=amount).update(
        balance=F("balance") - amount, updated_at=timezone.now()
    )
    if not updated:
        wallet.refresh_from_db(fields=["balance"])
        raise InsufficientBalance(
            "Insufficient wallet balance.", balance=wallet.balance, required=amount
        )
    wallet.refresh_from_db(fields=["balance", "updated_at"])
    return LedgerEntry.objects.create(
        wallet=wallet, amount=-amount, kind=kind, reference=reference, balance_after=wallet.balance
    )


def initiate_recharge(user, amount) -> RechargeTransaction:
    return RechargeTransaction.objects.create(user=user, amount=_money(amount))


@transaction.atomic
def process_recharge_callback(*, reference: str, status: str):
    """Apply a gateway callback. Returns (transaction, credited: bool).

    Idempotent: replaying the same callback is a no-op that returns credited=False.
    The wallet is credited by whichever call wins the PENDING -> SUCCESS claim, exactly once.
    The credited amount is the one stored at initiation, never one from the callback payload.
    """
    Status = RechargeTransaction.Status
    try:
        txn = RechargeTransaction.objects.select_for_update().get(reference=reference)
    except RechargeTransaction.DoesNotExist:
        raise NotFound("Unknown recharge reference.")

    if txn.status == status:  # replay of an already-applied callback
        return txn, False
    if txn.status != Status.PENDING:  # e.g. success after failed, or the reverse
        raise Conflict(
            f"Recharge is already '{txn.status}'; cannot change it to '{status}'.",
            current_status=txn.status,
        )

    # Atomic claim. Even without row locks (SQLite) only one caller can flip PENDING.
    claimed = RechargeTransaction.objects.filter(pk=txn.pk, status=Status.PENDING).update(
        status=status, updated_at=timezone.now()
    )
    txn.refresh_from_db()
    if not claimed:
        return txn, False

    credited = False
    if status == Status.SUCCESS:
        wallet = lock_wallet(txn.user)
        credit(wallet, txn.amount, kind=LedgerEntry.Kind.RECHARGE, reference=f"recharge:{txn.reference}")
        credited = True
    return txn, credited
