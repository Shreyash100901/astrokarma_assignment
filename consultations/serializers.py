from rest_framework import serializers

from .models import Session


class SessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Session
        fields = [
            "id", "user", "provider", "status", "per_minute_cost",
            "started_at", "ended_at", "billed_minutes", "billed_amount", "talk_seconds",
            "ended_by", "end_reason", "created_at",
        ]
        read_only_fields = fields


class AssignSerializer(serializers.Serializer):
    provider_id = serializers.IntegerField()
