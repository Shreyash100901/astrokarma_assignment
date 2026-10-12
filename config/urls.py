from django.urls import include, path
from django.conf import settings

urlpatterns = [
    path("api/auth/", include("accounts.urls")),
    path("api/wallet/", include("wallet.urls")),
    path("api/sessions/", include("consultations.urls")),
]


if settings.DEBUG:
    try:
        import playground
    except ImportError:
        pass
    else:
        urlpatterns += playground.urlpatterns