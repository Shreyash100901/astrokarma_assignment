from decimal import Decimal

from django.db import transaction
from rest_framework import serializers

from wallet.models import Wallet

from .models import ProviderProfile, User


class RegisterSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(write_only=True, min_length=8)
    role = serializers.ChoiceField(choices=User.Role.choices)
    per_minute_rate = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal("0.01"), required=False
    )

    def validate_username(self, value):
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError(
                "A user with this username already exists."
            )
        return value

    def validate(self, attrs):
        is_provider = attrs["role"] == User.Role.PROVIDER
        has_rate = "per_minute_rate" in attrs
        if is_provider and not has_rate:
            raise serializers.ValidationError(
                {"per_minute_rate": "Required for providers."}
            )
        if not is_provider and has_rate:
            raise serializers.ValidationError(
                {"per_minute_rate": "Only valid for providers."}
            )
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        rate = validated_data.pop("per_minute_rate", None)
        user = User.objects.create_user(**validated_data)
        if rate is not None:
            ProviderProfile.objects.create(user=user, per_minute_rate=rate)
        Wallet.objects.create(user=user)
        return user


class ProviderSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()
    per_minute_rate = serializers.DecimalField(
        source="provider_profile.per_minute_rate", max_digits=12, decimal_places=2
    )
