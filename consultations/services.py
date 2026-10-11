"""Session state machine + per-minute billing. All business rules live here; views are thin.

Billing policy (see DECISIONS.md): every *started* minute is charged, in advance.
    minutes_due = max(1, ceil(elapsed_seconds / 60))
Minute 1 is charged the moment the session starts. Minute N is charged once elapsed time
passes (N-1) minutes. A tick that arrives late catches up by billing each missed minute
individually, and stops at the first one the wallet cannot pay.
"""
import math
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from accounts.models import User
from common.exceptions import ActiveSessionExists, InsufficientBalance, InvalidTransition
from wallet import services as wallet_services
from wallet.models import LedgerEntry

from .models import Session, SessionBilling

S = Session.Status

# action -> {allowed from-state: resulting state}. The single source of truth for the lifecycle.
TRANSITIONS = {
    "assign": {S.UNASSIGNED: S.ASSIGNED},
    "accept": {S.ASSIGNED: S.ACCEPTED_BY_PROVIDER},
    "start": {S.ACCEPTED_BY_PROVIDER: S.IN_PROGRESS},
    "end": {S.IN_PROGRESS: S.COMPLETED},
    "bill": {S.IN_PROGRESS: S.IN_PROGRESS},
    "cancel": {
        S.UNASSIGNED: S.CANCELLED,
        S.ASSIGNED: S.CANCELLED,
        S.ACCEPTED_BY_PROVIDER: S.CANCELLED,
    },
}


@dataclass
class BillingResult:
    session: Session
    minutes_billed: int = 0
    amount_charged: Decimal = field(default_factory=lambda: Decimal("0.00"))


# ----------------------------------------------------------------------------- helpers
def _lock_session(session_id) -> Session:
    try:
        return Session.objects.select_for_update().get(pk=session_id)
    except Session.DoesNotExist:
        raise NotFound("Session not found.")


def _assert_transition(session: Session, action: str):
    """Always called on a row-locked session, so the status we check is the status we change."""
    allowed = TRANSITIONS[action]
    if session.status not in allowed:
        raise InvalidTransition(
            f"Cannot {action} a session that is '{session.status}'. "
            f"'{action}' is only allowed from: {', '.join(allowed)}.",
            action=action,
            current_status=session.status,
        )


def _is_owner(session, actor) -> bool:
    return actor.role == User.Role.USER and session.user_id == actor.id


def _is_assigned_provider(session, actor) -> bool:
    return actor.role == User.Role.PROVIDER and session.provider_id == actor.id


def _require_owner(session, actor):
    if not _is_owner(session, actor):
        raise PermissionDenied("Only the user who owns this session can do this.")


def _require_assigned_provider(session, actor):
    if not _is_assigned_provider(session, actor):
        raise PermissionDenied("Only the provider assigned to this session can do this.")


def _participant_role(session, actor) -> str:
    if _is_owner(session, actor):
        return Session.EndedBy.USER
    if _is_assigned_provider(session, actor):
        return Session.EndedBy.PROVIDER
    raise PermissionDenied("You are not a participant in this session.")


def _finish(session, *, ended_by, ended_at, talk_seconds, end_reason=""):
    session.status = S.COMPLETED
    session.ended_by = ended_by
    session.end_reason = end_reason
    session.ended_at = ended_at
    session.talk_seconds = talk_seconds
    session.save(
        update_fields=[
            "status", "ended_by", "end_reason", "ended_at", "talk_seconds",
            "billed_minutes", "billed_amount", "updated_at",
        ]
    )


def sessions_for(actor):
    return Session.objects.filter(Q(user=actor) | Q(provider=actor)).order_by("-id")


# ----------------------------------------------------------------------------- lifecycle
def create_session(actor) -> Session:
    if actor.role != User.Role.USER:
        raise PermissionDenied("Only users can create sessions.")
    return Session.objects.create(user=actor)


def assign_session(session_id, actor, provider_id) -> Session:
    with transaction.atomic():
        session = _lock_session(session_id)
        _require_owner(session, actor)
        _assert_transition(session, "assign")
        provider = User.objects.filter(
            pk=provider_id, role=User.Role.PROVIDER, provider_profile__isnull=False
        ).first()
        if provider is None:
            raise ValidationError({"provider_id": "No provider with this id."})
        session.provider = provider
        session.status = TRANSITIONS["assign"][session.status]
        session.save(update_fields=["provider", "status", "updated_at"])
    return session


def accept_session(session_id, actor) -> Session:
    with transaction.atomic():
        session = _lock_session(session_id)
        _require_assigned_provider(session, actor)
        _assert_transition(session, "accept")
        session.status = TRANSITIONS["accept"][session.status]
        session.save(update_fields=["status", "updated_at"])
    return session


def cancel_session(session_id, actor, now=None) -> Session:
    now = now or timezone.now()
    with transaction.atomic():
        session = _lock_session(session_id)
        role = _participant_role(session, actor)
        _assert_transition(session, "cancel")
        session.status = TRANSITIONS["cancel"][session.status]
        session.ended_by = role
        session.ended_at = now
        session.save(update_fields=["status", "ended_by", "ended_at", "updated_at"])
    return session


def start_session(session_id, actor, now=None) -> Session:
    now = now or timezone.now()
    with transaction.atomic():
        session = _lock_session(session_id)
        _require_owner(session, actor)
        _assert_transition(session, "start")

        # The wallet lock doubles as a per-user mutex: two concurrent starts for the same
        # user serialise here, so the "one active session" check below cannot be raced.
        wallet = wallet_services.lock_wallet(session.user)

        rate = session.provider.provider_profile.per_minute_rate
        required = rate * settings.MIN_BALANCE_MINUTES
        if wallet.balance < required:
            raise InsufficientBalance(
                f"Starting a session needs at least {settings.MIN_BALANCE_MINUTES} minutes of "
                f"balance ({required}); wallet has {wallet.balance}.",
                balance=wallet.balance,
                required=required,
            )

        if Session.objects.filter(user=session.user, status=S.IN_PROGRESS).exclude(pk=session.pk).exists():
            raise ActiveSessionExists("You already have a session in progress.")

        session.status = TRANSITIONS["start"][session.status]
        session.per_minute_cost = rate  # frozen for the life of the session
        session.started_at = now
        try:
            with transaction.atomic():  # savepoint, so an IntegrityError doesn't poison the outer txn
                session.save(update_fields=["status", "per_minute_cost", "started_at", "updated_at"])
        except IntegrityError:  # DB-level backstop for the one-active-session rule
            raise ActiveSessionExists("You already have a session in progress.")

        _bill_locked(session, wallet, now)  # charges minute 1; cannot fail (balance >= 5 x rate)
    return session


def end_session(session_id, actor, now=None) -> Session:
    """User or provider ends the call. A final billing pass runs first, in the same transaction."""
    now = now or timezone.now()
    with transaction.atomic():
        session = _lock_session(session_id)
        role = _participant_role(session, actor)
        _assert_transition(session, "end")
        wallet = wallet_services.lock_wallet(session.user)

        _bill_locked(session, wallet, now)

        # If that pass found the next minute unpayable, the system already ended the session
        # (and capped talk time at what was paid for). Otherwise end it normally now.
        if session.status == S.IN_PROGRESS:
            talk = int(max(0, (now - session.started_at).total_seconds()))
            _finish(session, ended_by=role, ended_at=now, talk_seconds=talk)
    return session


def bill_session(session_id, actor=None, now=None) -> BillingResult:
    """Bill an in-progress session up to `now`. Safe to call any number of times.

    actor=None means a system caller (cron / management command): no participant check.
    """
    now = now or timezone.now()
    with transaction.atomic():
        session = _lock_session(session_id)
        if actor is not None:
            _participant_role(session, actor)
        _assert_transition(session, "bill")
        wallet = wallet_services.lock_wallet(session.user)
        return _bill_locked(session, wallet, now)


def bill_all_active(now=None) -> dict:
    now = now or timezone.now()
    summary = {"checked": 0, "minutes_billed": 0, "auto_ended": 0}
    for pk in Session.objects.filter(status=S.IN_PROGRESS).values_list("pk", flat=True):
        try:
            result = bill_session(pk, now=now)
        except InvalidTransition:  # ended by someone else between listing and locking
            continue
        summary["checked"] += 1
        summary["minutes_billed"] += result.minutes_billed
        if result.session.status == S.COMPLETED:
            summary["auto_ended"] += 1
    return summary


# ----------------------------------------------------------------------------- billing core
def _bill_locked(session: Session, wallet, now) -> BillingResult:
    """Caller must hold row locks on `session` and `wallet` inside one transaction."""
    rate = session.per_minute_cost
    elapsed = max(0.0, (now - session.started_at).total_seconds())
    minutes_due = max(1, math.ceil(elapsed / 60))

    result = BillingResult(session)
    while session.billed_minutes < minutes_due:
        minute = session.billed_minutes + 1
        try:
            wallet_services.debit(
                wallet,
                rate,
                kind=LedgerEntry.Kind.SESSION_DEBIT,
                reference=f"session:{session.pk}:minute:{minute}",
            )
        except InsufficientBalance:
            # Cannot pay for this minute: end the session at the paid boundary. Wallet untouched.
            paid_seconds = session.billed_minutes * 60
            _finish(
                session,
                ended_by=Session.EndedBy.SYSTEM,
                end_reason=Session.EndReason.INSUFFICIENT_BALANCE,
                ended_at=session.started_at + timedelta(seconds=paid_seconds),
                talk_seconds=paid_seconds,
            )
            return result

        SessionBilling.objects.create(session=session, minute_index=minute, amount=rate, billed_at=now)
        session.billed_minutes = minute
        session.billed_amount += rate
        result.minutes_billed += 1
        result.amount_charged += rate

    if result.minutes_billed:
        session.save(update_fields=["billed_minutes", "billed_amount", "updated_at"])
    return result
