from rest_framework.authtoken.models import Token
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import User
from .serializers import ProviderSerializer, RegisterSerializer


class RegisterView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        token = Token.objects.create(user=user)
        return Response(
            {"id": user.id, "username": user.username, "role": user.role, "token": token.key},
            status=201,
        )


class ProviderListView(APIView):
    """Lets a user discover provider ids (and rates) to assign a session to."""

    def get(self, request):
        providers = User.objects.filter(
            role=User.Role.PROVIDER, provider_profile__isnull=False
        ).select_related("provider_profile")
        return Response(ProviderSerializer(providers, many=True).data)
