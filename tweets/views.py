from rest_framework import viewsets, permissions,status,filters,generics
from rest_framework.parsers import MultiPartParser, FormParser,JSONParser
from tweets.tasks import classify_image, moderate_content, notify_followers, resize_image
from .models import Tweet,Comment
from .serializers import CommentSerializer, TweetSerializer,UserSerializer,UserInfoSerializer
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
import redis
import os



class IsOwnerOrReadOnly(permissions.BasePermission):
    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return True
        return obj.user == request.user
    
# --- NEW: SIGNUP VIEW ---
class UserCreate(generics.CreateAPIView):
    queryset = UserSerializer.Meta.model.objects.all()
    serializer_class = UserSerializer
    permission_classes = [permissions.AllowAny]

# --- NEW: AVAILABILITY CHECK VIEW ---
@api_view(['POST'])
@permission_classes([permissions.AllowAny])
def check_availability(request):
    """
    Checks if a username or email is already taken.
    Expects JSON: { "type": "username", "value": "john" }
    """
    check_type = request.data.get('type') # 'username' or 'email'
    value = request.data.get('value')

    if not value:
        return Response({'error': 'Value is required'}, status=400)

    if check_type == 'username':
        if User.objects.filter(username__iexact=value).exists():
            return Response({'taken': True, 'message': 'Username already taken'}, status=200)
    
    elif check_type == 'email':
        if User.objects.filter(email__iexact=value).exists():
            return Response({'taken': True, 'message': 'Email already registered'}, status=200)

    return Response({'taken': False}, status=200)

# --- HEALTH CHECK ENDPOINTS (for Kubernetes Probes) ---
@api_view(['GET'])
@permission_classes([permissions.AllowAny])
def health_check(request):
    """Liveness probe — is the process alive?"""
    return Response({'status': 'healthy'}, status=200)

@api_view(['GET'])
@permission_classes([permissions.AllowAny])
def readiness_check(request):
    """
    Readiness probe — can the app serve traffic?
    Checks database and Redis connectivity.
    """
    errors = []
    
    # Check Database
    try:
        connection.ensure_connection()
    except Exception as e:
        errors.append(f'Database: {str(e)}')
    
    # Check Redis
    try:
        redis_host = os.environ.get('REDIS_HOST', 'redis')
        r = redis.Redis(host=redis_host, port=6379, socket_timeout=2)
        r.ping()
    except Exception as e:
        errors.append(f'Redis: {str(e)}')
    
    if errors:
        return Response({'status': 'unhealthy', 'errors': errors}, status=503)
    
    return Response({'status': 'ready', 'database': 'ok', 'redis': 'ok'}, status=200)


class TweetViewSet(viewsets.ModelViewSet):
    queryset = Tweet.objects.select_related('user').prefetch_related('comments', 'comments__user').order_by('-created_at')
    serializer_class = TweetSerializer
    
    # 1. Security: Users must be logged in to post, but anyone can read
    # IsOwnerOrReadOnly ensures only the author can Update/Delete
    permission_classes = [permissions.IsAuthenticatedOrReadOnly, IsOwnerOrReadOnly]

    # 2. The Magic: This tells Django "Expect files, not just JSON"
    parser_classes = (MultiPartParser, FormParser,JSONParser)

    # --- NEW: SEARCH CONFIGURATION ---
    filter_backends = [filters.SearchFilter]
    search_fields = ['content', 'user__username','ai_tags'] # Search by text or username

    # --- REDIS CACHING: Cache the feed listing ---
    def list(self, request, *args, **kwargs):
        """GET /api/tweets/ — cached for 60s to reduce DB load."""
        cache_key = f'tweet_feed_page_{request.query_params.get("page", 1)}'
        cached_response = cache.get(cache_key)
        
        if cached_response is not None:
            return Response(cached_response)
        
        response = super().list(request, *args, **kwargs)
        cache.set(cache_key, response.data, timeout=60)  # 60s TTL
        return response

    def _invalidate_feed_cache(self):
        """Clear all cached feed pages on data mutation."""
        # Clear first 20 pages of cache
        for page in range(1, 21):
            cache.delete(f'tweet_feed_page_{page}')

    # 3. Automation: Auto-assign the 'user' field when saving
    def perform_create(self, serializer):
        # 1. Save the raw tweet
        tweet = serializer.save(user=self.request.user)
        
        # Invalidate feed cache
        self._invalidate_feed_cache()
        
        # 2. Trigger the Background Pipeline
        # We pass the ID, not the whole object, because passing objects to Celery is risky
        
        # A. Notify Followers
        notify_followers.delay(tweet.user.username, tweet.content)
        
        # B. Check for bad words
        moderate_content.delay(tweet.id)
        
        # C. Resize Image (if one exists)
        if tweet.image:
            resize_image.delay(tweet.id)
            classify_image.delay(tweet.id)

    def perform_update(self, serializer):
        # Save the changes
        tweet = serializer.save()
        self._invalidate_feed_cache()
        
        # Re-run moderation because text might have changed!
        moderate_content.delay(tweet.id)

    def perform_destroy(self, instance):
        self._invalidate_feed_cache()
        super().perform_destroy(instance)
    # This creates a new URL: POST /api/tweets/{id}/share/
    @action(detail=True, methods=['post'])
    def share(self, request, pk=None):
        tweet = self.get_object() # Get the tweet by ID (pk)
        tweet.shares_count += 1   # Increment count
        tweet.save()              # Save to DB
        return Response({'status': 'shared', 'shares_count': tweet.shares_count})
    
    

class CommentViewSet(viewsets.ModelViewSet):
    queryset = Comment.objects.all().order_by('-created_at')
    serializer_class = CommentSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

# --- NEW: GET CURRENT USER VIEW ---
@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_current_user(request):
    serializer = UserInfoSerializer(request.user)
    return Response(serializer.data)