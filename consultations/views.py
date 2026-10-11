from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from . import services
from .serializers import AssignSerializer, SessionSerializer


def _data(session):
    return SessionSerializer(session).data


class SessionViewSet(viewsets.ViewSet):
    lookup_value_regex = r"\d+"

    def list(self, request):
        return Response(SessionSerializer(services.sessions_for(request.user), many=True).data)

    def retrieve(self, request, pk=None):
        session = services.sessions_for(request.user).filter(pk=pk).first()
        if session is None:
            raise NotFound("Session not found.")
        return Response(_data(session))

    def create(self, request):
        return Response(_data(services.create_session(request.user)), status=201)

    @action(detail=True, methods=["post"])
    def assign(self, request, pk=None):
        serializer = AssignSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        session = services.assign_session(pk, request.user, serializer.validated_data["provider_id"])
        return Response(_data(session))

    @action(detail=True, methods=["post"])
    def accept(self, request, pk=None):
        return Response(_data(services.accept_session(pk, request.user)))

    @action(detail=True, methods=["post"])
    def start(self, request, pk=None):
        return Response(_data(services.start_session(pk, request.user)))

    @action(detail=True, methods=["post"])
    def end(self, request, pk=None):
        return Response(_data(services.end_session(pk, request.user)))

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        return Response(_data(services.cancel_session(pk, request.user)))

    @action(detail=True, methods=["post"])
    def bill(self, request, pk=None):
        result = services.bill_session(pk, actor=request.user)
        return Response(
            {
                **_data(result.session),
                "minutes_billed_now": result.minutes_billed,
                "amount_charged_now": result.amount_charged,
            }
        )
