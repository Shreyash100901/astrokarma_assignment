import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q


def generate_reference():
    return f"rch_{uuid.uuid4().hex}"


class Wallet(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wallet"
    )
    balance = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # Last line of defence: the database itself refuses a negative balance.
            models.CheckConstraint(
                condition=Q(balance__gte=0), name="wallet_balance_non_negative"
            ),
        ]

    def __str__(self):
        return f"Wallet({self.user_id}: {self.balance})"


class RechargeTransaction(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        SUCCESS = "success"
        FAILED = "failed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="recharges"
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reference = models.CharField(max_length=64, unique=True, default=generate_reference)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0), name="recharge_amount_positive"
            ),
        ]


class LedgerEntry(models.Model):
    """Append-only audit trail. `reference` is an idempotency key: one money movement, one row."""

    class Kind(models.TextChoices):
        RECHARGE = "recharge"
        SESSION_DEBIT = "session_debit"

    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="entries")
    amount = models.DecimalField(
        max_digits=12, decimal_places=2
    )  # signed: + credit, - debit
    kind = models.CharField(max_length=20, choices=Kind.choices)
    reference = models.CharField(max_length=100, unique=True)
    balance_after = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
