from rest_framework.routers import SimpleRouter

from .views import SessionViewSet

router = SimpleRouter()
router.register("", SessionViewSet, basename="session")

urlpatterns = router.urls
