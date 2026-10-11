from django.urls import path
from rest_framework.authtoken.views import obtain_auth_token

from .views import ProviderListView, RegisterView

urlpatterns = [
    path("register/", RegisterView.as_view()),
    path("token/", obtain_auth_token),
    path("providers/", ProviderListView.as_view()),
]
