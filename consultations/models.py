from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q


class Session(models.Model):
    class Status(models.TextChoices):
        UNASSIGNED = "unassigned"
        ASSIGNED = "assigned"
        ACCEPTED_BY_PROVIDER = "accepted_by_provider"
        IN_PROGRESS = "in_progress"
        COMPLETED = "completed"
        CANCELLED = "cancelled"

    class EndedBy(models.TextChoices):
        USER = "user"
        PROVIDER = "provider"
        SYSTEM = "system"

    class EndReason(models.TextChoices):
        INSUFFICIENT_BALANCE = "insufficient_balance", "Insufficient Balance"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="sessions_as_user")
    provider = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="sessions_as_provider",
    )
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.UNASSIGNED, db_index=True)

    # Snapshotted from the provider's rate when the session starts; never changes afterwards.
    per_minute_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    # Billing progress. billed_minutes is the high-water mark that prevents re-charging a minute.
    billed_minutes = models.PositiveIntegerField(default=0)
    billed_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    talk_seconds = models.PositiveIntegerField(default=0)

    ended_by = models.CharField(max_length=16, choices=EndedBy.choices, blank=True)
    end_reason = models.CharField(max_length=32, choices=EndReason.choices, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # A user can have at most one in-progress session, enforced by the database.
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(status="in_progress"),
                name="one_active_session_per_user",
            ),
        ]


class SessionBilling(models.Model):
    """One row per charged minute. The unique constraint makes double-charging a minute impossible."""

    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name="billings")
    minute_index = models.PositiveIntegerField()  # 1-based
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    billed_at = models.DateTimeField()

    class Meta:
        ordering = ["minute_index"]
        constraints = [
            models.UniqueConstraint(fields=["session", "minute_index"], name="uniq_billing_per_session_minute"),
        ]
