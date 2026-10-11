from django.urls import path

from .views import RechargeCallbackView, RechargeCreateView, WalletView

urlpatterns = [
    path("", WalletView.as_view()),
    path("recharge/", RechargeCreateView.as_view()),
    path("recharge/callback/", RechargeCallbackView.as_view()),
]
