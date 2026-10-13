from rest_framework import generics, status, permissions
from rest_framework.response import Response
from drf_spectacular.utils import extend_schema
from .serializers import ReviewCreateSerializer
from apps.adoptions.serializers import AdoptionReviewDetailSerializer


class ReviewCreateAPIView(generics.CreateAPIView):
    """
    Submit dual rating review for shelter care and delivery transit.
    POST /api/reviews/
    """
    serializer_class = ReviewCreateSerializer
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="Submit post-delivery review",
        description="Customer submits 1-5 star dual ratings for shelter and delivery partner.",
        request=ReviewCreateSerializer,
        responses={201: AdoptionReviewDetailSerializer}
    )
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        review = serializer.save()
        response_data = AdoptionReviewDetailSerializer(review).data
        return Response({
            "message": "Review submitted successfully.",
            "review": response_data
        }, status=status.HTTP_201_CREATED)
