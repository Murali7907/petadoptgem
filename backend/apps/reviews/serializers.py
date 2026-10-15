from rest_framework import serializers
from apps.adoptions.models import AdoptionApplication, AdoptionReview


class ReviewCreateSerializer(serializers.ModelSerializer):
    application_id = serializers.CharField(write_only=True)
    shelter_rating = serializers.IntegerField(min_value=1, max_value=5, default=5)
    shelter_review = serializers.CharField(required=False, allow_blank=True, default='')
    driver_rating = serializers.IntegerField(min_value=1, max_value=5, default=5)
    driver_review = serializers.CharField(required=False, allow_blank=True, default='')

    class Meta:
        model = AdoptionReview
        fields = [
            'id',
            'public_id',
            'application_id',
            'shelter_rating',
            'shelter_review',
            'driver_rating',
            'driver_review',
            'created_at',
        ]
        read_only_fields = ['id', 'public_id', 'created_at']

    def validate_application_id(self, value):
        import uuid
        app = None
        try:
            val = uuid.UUID(str(value))
            app = AdoptionApplication.objects.filter(public_id=val).first()
        except (ValueError, TypeError, AttributeError):
            pass
        if not app and str(value).isdigit():
            app = AdoptionApplication.objects.filter(id=int(value)).first()
        if not app:
            app = AdoptionApplication.objects.filter(public_id=value).first()
        if not app:
            raise serializers.ValidationError("Adoption application not found.")
        return app

    def validate(self, attrs):
        request = self.context.get('request')
        app = attrs['application_id']
        from rest_framework.exceptions import PermissionDenied
        if not request or not request.user or app.applicant_id != request.user.id:
            raise PermissionDenied("You are not authorized to review this application.")

        delivery = getattr(app, 'delivery', None)
        if not delivery or delivery.status != 'delivered':
            raise serializers.ValidationError("Reviews can only be submitted for completed deliveries.")

        if AdoptionReview.objects.filter(application=app).exists():
            raise serializers.ValidationError("A review has already been submitted for this application.")

        return attrs

    def create(self, validated_data):
        app = validated_data.pop('application_id')
        user = self.context['request'].user
        review = AdoptionReview.objects.create(
            application=app,
            reviewer=user,
            rating=validated_data.get('shelter_rating', 5),
            comment=validated_data.get('shelter_review', ''),
            **validated_data
        )
        return review
