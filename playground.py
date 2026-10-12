"""Dev-only helpers behind playground.html (served at /play/). Not part of the assignment API."""
import json
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import F, Sum
from django.http import FileResponse, JsonResponse
from django.urls import path
from django.utils import timezone
from django.views.decorators.http import require_POST
from rest_framework.authtoken.models import Token

from accounts.models import ProviderProfile, User
from consultations import services
from consultations.models import Session, SessionBilling
from wallet.models import LedgerEntry, RechargeTransaction, Wallet


def page(request):
    return FileResponse(open(settings.BASE_DIR / "playground.html", "rb"), content_type="text/html")


def state(request):
    names = {u.id: u.username for u in User.objects.all()}
    rates = {p.user_id: p.per_minute_rate for p in ProviderProfile.objects.all()}
    wallets = list(Wallet.objects.all())
    wname = {w.id: names[w.user_id] for w in wallets}
    sums = {r["wallet_id"]: r["t"] for r in LedgerEntry.objects.values("wallet_id").annotate(t=Sum("amount"))}

    def named(row, *keys):
        for k in keys:
            row[k[:-3]] = names.get(row.pop(k))
        return row

    return JsonResponse({
        "now": timezone.now(),
        "users": [{"id": u.id, "username": u.username, "role": u.role, "rate": rates.get(u.id)}
                  for u in User.objects.order_by("id")],
        "wallets": [{"user": names[w.user_id], "balance": w.balance, "ledger_sum": sums.get(w.id, Decimal("0.00"))}
                    for w in wallets],
        "sessions": [named(r, "user_id", "provider_id") for r in Session.objects.order_by("id").values()],
        "recharges": [named(r, "user_id") for r in RechargeTransaction.objects.order_by("-id").values()[:40]],
        "ledger": [{"id": e["id"], "wallet": wname[e["wallet_id"]], "amount": e["amount"], "kind": e["kind"],
                    "reference": e["reference"], "balance_after": e["balance_after"]}
                   for e in LedgerEntry.objects.order_by("-id").values()[:60]],
        "billings": list(SessionBilling.objects.order_by("-id").values("id", "session_id", "minute_index", "amount")[:60]),
    })


@require_POST
def time_travel(request):
    b = json.loads(request.body)
    n = Session.objects.filter(pk=b["session_id"], status="in_progress").update(
        started_at=F("started_at") - timedelta(minutes=int(b["minutes"])))
    if not n:
        return JsonResponse({"error": "Session must be in_progress to time-travel."}, status=400)
    return JsonResponse({"shifted_minutes": b["minutes"]})


@require_POST
def bill_all(request):
    return JsonResponse(services.bill_all_active())


@require_POST
def set_rate(request):
    b = json.loads(request.body)
    n = ProviderProfile.objects.filter(user_id=b["user_id"]).update(per_minute_rate=Decimal(str(b["rate"])))
    return JsonResponse({"updated": n}, status=200 if n else 400)


@require_POST
def reset(request):
    for m in (SessionBilling, Session, LedgerEntry, RechargeTransaction, Wallet, Token, ProviderProfile, User):
        m.objects.all().delete()
    return JsonResponse({"reset": True})


urlpatterns = [
    path("play/", page),
    path("play/api/state/", state),
    path("play/api/time-travel/", time_travel),
    path("play/api/bill-all/", bill_all),
    path("play/api/set-rate/", set_rate),
    path("play/api/reset/", reset),
]
