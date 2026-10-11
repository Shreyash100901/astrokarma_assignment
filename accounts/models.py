from decimal import Decimal

from django.contrib.auth.models import AbstractUser
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


class User(AbstractUser):
    class Role(models.TextChoices):
        USER = "USER"
        PROVIDER = "PROVIDER"

    role = models.CharField(max_length=10, choices=Role.choices, default=Role.USER)


class ProviderProfile(models.Model):
    """Providers set their own per-minute rate. It is snapshotted onto a session at start."""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="provider_profile")
    per_minute_rate = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(per_minute_rate__gt=0), name="provider_rate_positive"
            )
        ]

    def __str__(self):
        return f"{self.user.username} @ {self.per_minute_rate}/min"
