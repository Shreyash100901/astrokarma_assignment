from decimal import Decimal

from rest_framework import serializers

from .models import RechargeTransaction, Wallet


class WalletSerializer(serializers.ModelSerializer):
    class Meta:
        model = Wallet
        fields = ["user", "balance"]


class RechargeCreateSerializer(serializers.Serializer):
    amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal("0.01"), max_value=Decimal("1000000")
    )


class RechargeSerializer(serializers.ModelSerializer):
    class Meta:
        model = RechargeTransaction
        fields = ["reference", "amount", "status", "created_at", "updated_at"]


class CallbackSerializer(serializers.Serializer):
    reference = serializers.CharField(max_length=64)
    status = serializers.ChoiceField(
        choices=[RechargeTransaction.Status.SUCCESS, RechargeTransaction.Status.FAILED]
    )
