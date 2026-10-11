import hmac

from django.conf import settings
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import Wallet
from .serializers import (
    CallbackSerializer,
    RechargeCreateSerializer,
    RechargeSerializer,
    WalletSerializer,
)


class WalletView(APIView):
    def get(self, request):
        wallet, _ = Wallet.objects.get_or_create(user=request.user)
        return Response(WalletSerializer(wallet).data)


class RechargeCreateView(APIView):
    def post(self, request):
        serializer = RechargeCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        txn = services.initiate_recharge(request.user, serializer.validated_data["amount"])
        return Response(RechargeSerializer(txn).data, status=201)


class RechargeCallbackView(APIView):
    """Mock payment-gateway webhook. Authenticated by a shared secret, not a user token."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        supplied = request.headers.get("X-Webhook-Secret", "").encode()
        expected = settings.MOCK_WEBHOOK_SECRET.encode()
        if not hmac.compare_digest(supplied, expected):
            raise PermissionDenied("Invalid webhook secret.")

        serializer = CallbackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        txn, credited = services.process_recharge_callback(**serializer.validated_data)
        return Response({**RechargeSerializer(txn).data, "credited": credited})
