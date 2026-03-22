from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import TweetViewSet,CommentViewSet, UserCreate, check_availability, get_current_user, health_check, readiness_check

router = DefaultRouter()
router.register(r'tweets', TweetViewSet)
router.register(r'comments', CommentViewSet)

urlpatterns = [
    path('', include(router.urls)),
    path('signup/', UserCreate.as_view(), name='user-create'),
    path('check-availability/', check_availability, name='check_availability'),
    path('me/', get_current_user, name='get_current_user'),
    path('health/', health_check, name='health_check'),
    path('ready/', readiness_check, name='readiness_check'),
]