import sys
from pathlib import Path

# Ensure backend directory is in sys.path for IDE module resolution
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from django.shortcuts import get_object_or_404
from rest_framework import viewsets, generics, status, permissions
from rest_framework.views import APIView
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, NotFound
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.filters import SearchFilter, OrderingFilter
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
from django.db.models import F, Value, FloatField, Expression, Q
from django.db.models.functions import Radians, Sin, Cos, ASin, Sqrt, Power
from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiParameter, OpenApiTypes
from .models import Pet, PetImage, PetMedicalRecord, FavoritePet
from .filters import PetFilter
from .permissions import IsShelterOwner
from .serializers import (
    PetListSerializer,
    PetDetailSerializer,
    PetWriteSerializer,
    PetImageSerializer,
    PetMedicalRecordSerializer,
    PublicPetListSerializer,
    PublicPetDetailSerializer,
    PetSummarySerializer,
    FavoritePetSerializer,
)
from apps.accounts.permissions import IsShelterRole, IsAdminRole, IsVerifiedShelter
from apps.accounts.models import ShelterProfile
from .storage import get_document_storage

class Point:
    """
    Representation of a geographic point (lng, lat) with SRID (default EPSG:4326).
    """
    def __init__(self, x, y, srid=4326):
        self.x = float(x)
        self.y = float(y)
        self.lng = float(x)
        self.lat = float(y)
        self.srid = srid

    def __repr__(self):
        return f"Point({self.x}, {self.y}, srid={self.srid})"


class Distance(Expression):
    """
    Geodesic distance calculation in meters between shelter (latitude, longitude)
    and a reference Point using the spherical Haversine formula.
    Provides drop-in Distance('shelter__location', Point(...)) interface with distance computed with the Haversine formula in SQL.
    """
    def __init__(self, field_name, point, output_field=None):
        self.field_name = field_name
        self.point = point
        super().__init__(output_field=output_field or FloatField())

        lat1 = Radians(Value(point.lat, output_field=FloatField()))
        lng1 = Radians(Value(point.lng, output_field=FloatField()))
        lat2 = Radians(F('shelter__latitude'))
        lng2 = Radians(F('shelter__longitude'))

        dlat = (lat2 - lat1) / 2.0
        dlng = (lng2 - lng1) / 2.0

        a = Power(Sin(dlat), 2) + Cos(lat1) * Cos(lat2) * Power(Sin(dlng), 2)
        self.haversine_expr = Value(6371000.0, output_field=FloatField()) * 2.0 * ASin(Sqrt(a))

    def resolve_expression(self, query=None, allow_joins=True, reuse=None, summarize=False, for_save=False):
        c = self.copy()
        c.haversine_expr = self.haversine_expr.resolve_expression(query, allow_joins, reuse, summarize, for_save)
        return c

    def as_sql(self, compiler, connection):
        return self.haversine_expr.as_sql(compiler, connection)


class PetViewSet(viewsets.ModelViewSet):
    """
    Public ViewSet for viewing available pets.
    """
    lookup_field = 'public_id'
    queryset = Pet.objects.all().select_related('shelter').prefetch_related('images', 'medical_records')
    filterset_class = PetFilter
    search_fields = ['name', 'breed', 'description', 'shelter__organization_name']
    ordering_fields = ['created_at', 'adoption_fee', 'age_months', 'name']
    ordering = ['-created_at']

    def get_queryset(self):
        user = self.request.user if hasattr(self, 'request') else None
        qs = Pet.objects.all().select_related('shelter', 'shelter__user').prefetch_related('images', 'medical_records')
        if user and user.is_authenticated:
            if user.is_superuser or user.role == 'admin':
                pass
            elif user.role == 'shelter' and hasattr(user, 'shelter_profile'):
                qs = qs.filter(Q(shelter=user.shelter_profile) | Q(shelter__is_verified=True))
            else:
                qs = qs.filter(shelter__is_verified=True)
                if self.action == 'list':
                    qs = qs.filter(status='available')
        else:
            qs = qs.filter(shelter__is_verified=True)
            if self.action == 'list':
                qs = qs.filter(status='available')

        if hasattr(self, 'request') and self.request:
            lat = self.request.query_params.get('lat')
            lng = self.request.query_params.get('lng')
            if lat is not None and lng is not None:
                try:
                    lat_val = float(lat)
                    lng_val = float(lng)
                    qs = qs.annotate(
                        distance_meters=Distance('shelter__location', Point(float(lng_val), float(lat_val), srid=4326))
                    ).order_by('distance_meters')
                except (ValueError, TypeError):
                    pass
        return qs

    def get_permissions(self):
        if self.action in ['list', 'retrieve']:
            return [permissions.AllowAny()]
        return [IsVerifiedShelter()]

    def get_serializer_class(self):
        if self.action == 'list':
            return PublicPetListSerializer
        elif self.action in ['create', 'update', 'partial_update']:
            return PetWriteSerializer
        return PublicPetDetailSerializer

    def get_object(self):
        queryset = self.filter_queryset(self.get_queryset())
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        lookup_val = self.kwargs[lookup_url_kwarg]

        obj = None
        try:
            import uuid
            uuid_val = uuid.UUID(str(lookup_val))
            obj = queryset.filter(public_id=uuid_val).first()
        except (ValueError, TypeError, AttributeError):
            pass

        if not obj and str(lookup_val).isdigit():
            obj = queryset.filter(id=int(lookup_val)).first()

        if not obj:
            raise NotFound("Pet not found.")

        self.check_object_permissions(self.request, obj)
        return obj

    def perform_create(self, serializer):
        user = self.request.user
        shelter = getattr(user, 'shelter_profile', None)
        if (user.is_superuser or user.role == 'admin') and not shelter:
            shelter_id = self.request.data.get('shelter') or self.request.data.get('shelter_id')
            if shelter_id:
                shelter = ShelterProfile.objects.filter(id=shelter_id).first()
            if not shelter:
                raise PermissionDenied("Admin user must have a ShelterProfile associated or specify a valid shelter to post pets.")
        elif not shelter:
            raise PermissionDenied("Only registered shelters can create pet listings.")

        serializer.save(shelter=shelter)


class ShelterPetViewSet(viewsets.ModelViewSet):
    """
    Dedicated multi-tenant ViewSet for Shelter users to manage their own pets,
    multi-image galleries, and medical history records.
    """
    lookup_field = 'public_id'
    permission_classes = [IsShelterOwner]
    filterset_class = PetFilter

    def get_permissions(self):
        if self.action in ['list', 'retrieve'] and self.request.method in permissions.SAFE_METHODS:
            return [IsShelterOwner()]
        if self.action in ['medical', 'images']:
            if self.request.method in permissions.SAFE_METHODS:
                return [IsShelterOwner()]
            return [IsVerifiedShelter(), IsShelterOwner()]
        if self.action in ['create', 'update', 'partial_update', 'destroy', 'delete_image']:
            return [IsVerifiedShelter(), IsShelterOwner()]
        return [IsVerifiedShelter(), IsShelterOwner()]
    search_fields = ['name', 'breed', 'description']
    ordering_fields = ['created_at', 'adoption_fee', 'age_months', 'name']
    ordering = ['-created_at']

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return Pet.objects.none()
        if user.is_superuser:
            return Pet.objects.all().select_related('shelter').prefetch_related('images', 'medical_records')
        if hasattr(user, 'shelter_profile'):
            return Pet.objects.filter(shelter=user.shelter_profile).select_related('shelter').prefetch_related('images', 'medical_records')
        return Pet.objects.none()

    def get_serializer_class(self):
        if self.action == 'list':
            return PetListSerializer
        elif self.action in ['create', 'update', 'partial_update']:
            return PetWriteSerializer
        return PetDetailSerializer

    def perform_create(self, serializer):
        shelter = self.request.user.shelter_profile
        pet = serializer.save(shelter=shelter)

        # Auto-create initial medical record entry
        PetMedicalRecord.objects.create(
            pet=pet,
            title='Initial Intake Examination',
            description=f'Initial health checkup upon arrival at {shelter.organization_name}.',
            vet_name='Shelter Veterinary Staff',
            record_date=timezone.now().date()
        )

    @action(detail=True, methods=['get', 'put', 'patch'], url_path='medical')
    def medical(self, request, public_id=None):
        pet = self.get_object()
        medical_record = pet.medical_records.first()

        if request.method == 'GET':
            if not medical_record:
                return Response([], status=status.HTTP_200_OK)
            serializer = PetMedicalRecordSerializer(medical_record, context={'request': request})
            return Response(serializer.data)

        elif request.method in ['PUT', 'PATCH']:
            doc_file = request.FILES.get('document') or request.FILES.get('file')
            data = request.data.copy() if hasattr(request.data, 'copy') else dict(request.data)
            if doc_file:
                from .validators import validate_medical_document
                validate_medical_document(doc_file)
                from .storage import get_document_storage
                doc_storage = get_document_storage()
                saved_path = doc_storage.save(f"medical/{pet.id}/{doc_file.name}", doc_file)
                doc_url = doc_storage.url(saved_path)
                curr_desc = data.get('description', '')
                if not curr_desc and medical_record:
                    curr_desc = medical_record.clinical_description
                data['description'] = f"{curr_desc}\nDocument: {doc_url}".strip() if curr_desc else f"Document: {doc_url}"
            elif medical_record and getattr(medical_record, 'document', None):
                if 'description' in data and "Document: " not in data['description']:
                    data['description'] = f"{data['description']}\nDocument: {medical_record.document}".strip()

            if not medical_record:
                serializer = PetMedicalRecordSerializer(data=data, context={'request': request})
                serializer.is_valid(raise_exception=True)
                serializer.save(pet=pet)
                return Response(serializer.data, status=status.HTTP_201_CREATED)
            else:
                partial = (request.method == 'PATCH')
                serializer = PetMedicalRecordSerializer(medical_record, data=data, partial=partial, context={'request': request})
                serializer.is_valid(raise_exception=True)
                serializer.save()
                return Response(serializer.data)

    @action(detail=True, methods=['get', 'post'], url_path='images', parser_classes=[MultiPartParser, FormParser, JSONParser])
    def images(self, request, public_id=None):
        pet = self.get_object()

        if request.method == 'GET':
            serializer = PetImageSerializer(pet.images.all(), many=True)
            return Response(serializer.data)

        elif request.method == 'POST':
            from django.core.files.storage import default_storage
            image_files = request.FILES.getlist('images') or request.FILES.getlist('image')
            created_images = []

            if image_files:
                from .validators import validate_pet_image
                for img_file in image_files:
                    validate_pet_image(img_file)
                caption = request.data.get('caption', f"Gallery image for {pet.name}")
                is_primary = str(request.data.get('is_primary', 'false')).lower() in ('true', '1') if isinstance(request.data.get('is_primary'), str) else bool(request.data.get('is_primary', False))
                for img_file in image_files:
                    saved_path = default_storage.save(f"pets/{pet.id}/{img_file.name}", img_file)
                    file_url = default_storage.url(saved_path)
                    img_obj = PetImage.objects.create(
                        pet=pet,
                        image_url=request.build_absolute_uri(file_url),
                        caption=caption,
                        is_primary=is_primary
                    )
                    created_images.append(img_obj)
            elif 'image_url' in request.data:
                img_obj = PetImage.objects.create(
                    pet=pet,
                    image_url=request.data['image_url'],
                    caption=request.data.get('caption', f"Gallery image for {pet.name}"),
                    is_primary=str(request.data.get('is_primary', 'false')).lower() in ('true', '1') if isinstance(request.data.get('is_primary'), str) else bool(request.data.get('is_primary', False))
                )
                created_images.append(img_obj)
            else:
                return Response(
                    {"error": "No image file ('images') or 'image_url' supplied in request."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            serializer = PetImageSerializer(created_images, many=True)
            return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['delete'], url_path=r'images/(?P<image_id>[^/]+)')
    def delete_image(self, request, public_id=None, image_id=None):
        pet = self.get_object()
        try:
            if image_id.isdigit():
                image_obj = pet.images.get(id=int(image_id))
            else:
                image_obj = pet.images.get(public_id=image_id)
        except PetImage.DoesNotExist:
            raise NotFound("Pet image not found.")

        image_obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


def resolve_pet_from_kwargs(kwargs):
    pet_identifier = kwargs.get('pet_public_id') or kwargs.get('pet_id') or kwargs.get('id') or kwargs.get('pk')
    pet = None
    try:
        import uuid
        val = uuid.UUID(str(pet_identifier))
        pet = Pet.objects.filter(public_id=val).first()
    except (ValueError, TypeError, AttributeError):
        pass
    if not pet and str(pet_identifier).isdigit():
        pet = Pet.objects.filter(id=int(pet_identifier)).first()
    if not pet:
        pet = Pet.objects.filter(public_id=pet_identifier).first()
    if not pet:
        raise NotFound("Pet not found.")
    return pet


class PetImageCreateView(generics.CreateAPIView):
    """
    Upload and add an image or multiple images to a pet gallery.
    POST /api/pets/<id>/images/
    """
    serializer_class = PetImageSerializer
    permission_classes = [IsVerifiedShelter]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def create(self, request, *args, **kwargs):
        from django.core.files.storage import default_storage
        pet = resolve_pet_from_kwargs(self.kwargs)

        if not (self.request.user.is_superuser or self.request.user.role == 'admin'):
            if not hasattr(self.request.user, 'shelter_profile') or pet.shelter != self.request.user.shelter_profile:
                raise PermissionDenied("You can only add images to your own shelter's pets.")

        # Support multi-image files in request.FILES
        files = request.FILES.getlist('images') or request.FILES.getlist('image') or request.FILES.getlist('file')
        if files:
            from .validators import validate_pet_image
            for f in files:
                validate_pet_image(f)
            created_images = []
            caption = request.data.get('caption', f"Gallery image for {pet.name}")
            is_primary = str(request.data.get('is_primary', 'false')).lower() in ('true', '1') if isinstance(request.data.get('is_primary'), str) else bool(request.data.get('is_primary', False))
            for f in files:
                saved_path = default_storage.save(f"pets/{pet.id}/{f.name}", f)
                file_url = default_storage.url(saved_path)
                img_obj = PetImage.objects.create(
                    pet=pet,
                    image_url=request.build_absolute_uri(file_url),
                    caption=caption,
                    is_primary=is_primary
                )
                created_images.append(img_obj)
            return Response(PetImageSerializer(created_images, many=True).data, status=status.HTTP_201_CREATED)

        # Support array of image dicts in request.data['images']
        if isinstance(request.data.get('images'), list):
            created_images = []
            for item in request.data['images']:
                serializer = self.get_serializer(data=item)
                serializer.is_valid(raise_exception=True)
                img_obj = serializer.save(pet=pet)
                created_images.append(img_obj)
            return Response(PetImageSerializer(created_images, many=True).data, status=status.HTTP_201_CREATED)

        # Standard single image upload fallback
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(pet=pet)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class PetMedicalRecordListCreateView(generics.ListCreateAPIView):
    """
    List and create medical records for a pet.
    POST /api/pets/<id>/medical/
    """
    serializer_class = PetMedicalRecordSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_permissions(self):
        if self.request.method in permissions.SAFE_METHODS:
            return [permissions.AllowAny()]
        return [IsVerifiedShelter()]

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return PetMedicalRecord.objects.none()
        pet = resolve_pet_from_kwargs(self.kwargs)
        return PetMedicalRecord.objects.filter(pet=pet).order_by('-record_date')

    def perform_create(self, serializer):
        pet = resolve_pet_from_kwargs(self.kwargs)
        if not (self.request.user.is_superuser or self.request.user.role == 'admin'):
            if not hasattr(self.request.user, 'shelter_profile') or pet.shelter != self.request.user.shelter_profile:
                raise PermissionDenied("You can only add medical records to your own shelter's pets.")

        doc_file = self.request.FILES.get('document') or self.request.FILES.get('file')
        if doc_file and 'document' not in serializer.validated_data:
            from .validators import validate_medical_document
            validate_medical_document(doc_file)
            from .storage import get_document_storage
            doc_storage = get_document_storage()
            saved_path = doc_storage.save(f"medical/{pet.id}/{doc_file.name}", doc_file)
            doc_url = doc_storage.url(saved_path)
            desc = serializer.validated_data.get('description', '')
            desc_with_doc = f"{desc}\nDocument: {doc_url}".strip() if desc else f"Document: {doc_url}"
            serializer.save(pet=pet, description=desc_with_doc)
        else:
            serializer.save(pet=pet)


@extend_schema_view(
    list=extend_schema(
        summary="Browse available pets",
        description="Public discovery catalog of available pets. Supports filtering by species, breed, gender, size, age ranges, and shelter city. If lat and lng are provided, results are ordered by geographic proximity (Haversine formula).",
        parameters=[
            OpenApiParameter('lat', OpenApiTypes.FLOAT, description='Latitude for proximity sorting'),
            OpenApiParameter('lng', OpenApiTypes.FLOAT, description='Longitude for proximity sorting'),
        ],
        responses={200: PublicPetListSerializer(many=True)},
    ),
    retrieve=extend_schema(
        summary="Retrieve detailed pet profile",
        description="Public view of full pet profile, all images, sanitized medical overview, and shelter details.",
        responses={200: PublicPetDetailSerializer},
    ),
)
class PublicPetViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Public ViewSet allowing unauthenticated or authenticated customers to discover pets,
    filter by attributes, and sort by geographical proximity.
    """
    permission_classes = [permissions.AllowAny]
    lookup_field = 'public_id'
    filterset_class = PetFilter
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    search_fields = ['name', 'breed', 'description', 'shelter__organization_name']
    ordering_fields = ['created_at', 'adoption_fee', 'age_months', 'name', 'distance_meters']
    ordering = ['-created_at']

    def get_queryset(self):
        queryset = Pet.objects.all().select_related('shelter', 'shelter__user').prefetch_related('images', 'medical_records')
        user = self.request.user if hasattr(self, 'request') else None
        if user and user.is_authenticated:
            if user.is_superuser or user.role == 'admin':
                pass
            elif user.role == 'shelter' and hasattr(user, 'shelter_profile'):
                queryset = queryset.filter(Q(shelter=user.shelter_profile) | Q(shelter__is_verified=True))
            else:
                queryset = queryset.filter(shelter__is_verified=True)
                if self.action == 'list':
                    queryset = queryset.filter(status='available')
        else:
            queryset = queryset.filter(shelter__is_verified=True)
            if self.action == 'list':
                queryset = queryset.filter(status='available')

        if hasattr(self, 'request') and self.request:
            lat = self.request.query_params.get('lat')
            lng = self.request.query_params.get('lng')
            if lat is not None and lng is not None:
                try:
                    lat_val = float(lat)
                    lng_val = float(lng)
                    queryset = queryset.annotate(
                        distance_meters=Distance('shelter__location', Point(float(lng_val), float(lat_val), srid=4326))
                    ).order_by('distance_meters')
                except (ValueError, TypeError):
                    pass

        return queryset

    def get_object(self):
        queryset = self.filter_queryset(self.get_queryset())
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        lookup_val = self.kwargs[lookup_url_kwarg]

        obj = None
        try:
            import uuid
            uuid_val = uuid.UUID(str(lookup_val))
            obj = queryset.filter(public_id=uuid_val).first()
        except (ValueError, TypeError, AttributeError):
            pass

        if not obj and str(lookup_val).isdigit():
            obj = queryset.filter(id=int(lookup_val)).first()

        if not obj:
            raise NotFound("Pet not found.")

        self.check_object_permissions(self.request, obj)
        return obj

    def filter_queryset(self, queryset):
        qs = super().filter_queryset(queryset)
        if hasattr(self, 'request') and self.request:
            lat = self.request.query_params.get('lat')
            lng = self.request.query_params.get('lng')
            ordering_param = self.request.query_params.get('ordering')
            if lat is not None and lng is not None and not ordering_param:
                qs = qs.order_by('distance_meters')
        return qs

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return PublicPetDetailSerializer
        return PublicPetListSerializer


class CustomerFavoritePetsView(APIView):
    """
    List customer's favorited pets with summary serializer.
    GET /api/v1/customer/favorites/
    """
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="List customer favorite pets",
        description="Retrieve all pets saved to favorites by the authenticated customer.",
        responses={200: PetSummarySerializer(many=True)}
    )
    def get(self, request):
        pets = Pet.objects.filter(
            favorited_by__user=request.user
        ).select_related('shelter').prefetch_related('images').order_by('-favorited_by__created_at')
        serializer = PetSummarySerializer(pets, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)


class CustomerFavoritePetActionView(APIView):
    """
    Toggle, add, or remove pet from customer favorites.
    POST   /api/v1/customer/favorites/<uuid:pet_public_id>/
    DELETE /api/v1/customer/favorites/<uuid:pet_public_id>/
    """
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="Toggle or add pet to favorites",
        description="Toggle favorite status or explicitly add pet to customer favorites.",
        request=None,
        responses={
            200: OpenApiTypes.OBJECT,
            201: OpenApiTypes.OBJECT,
        }
    )
    def post(self, request, pet_public_id):
        pet = get_object_or_404(Pet, public_id=pet_public_id)
        fav = FavoritePet.objects.filter(user=request.user, pet=pet).first()
        action = request.data.get('action') or request.query_params.get('action')

        if fav:
            if action == 'add':
                return Response({
                    'message': 'Pet is already in favorites.',
                    'is_favorited': True,
                    'pet': PetSummarySerializer(pet, context={'request': request}).data
                }, status=status.HTTP_200_OK)
            # Default toggle removes if already present
            fav.delete()
            return Response({
                'message': 'Pet removed from favorites.',
                'is_favorited': False,
                'pet': PetSummarySerializer(pet, context={'request': request}).data
            }, status=status.HTTP_200_OK)
        else:
            FavoritePet.objects.create(user=request.user, pet=pet)
            return Response({
                'message': 'Pet added to favorites.',
                'is_favorited': True,
                'pet': PetSummarySerializer(pet, context={'request': request}).data
            }, status=status.HTTP_201_CREATED)

    @extend_schema(
        summary="Remove pet from favorites",
        description="Remove a pet from the customer's favorites by public_id.",
        responses={200: OpenApiTypes.OBJECT}
    )
    def delete(self, request, pet_public_id):
        pet = get_object_or_404(Pet, public_id=pet_public_id)
        fav = FavoritePet.objects.filter(user=request.user, pet=pet).first()
        if fav:
            fav.delete()
            return Response({
                'message': 'Pet removed from favorites.',
                'is_favorited': False
            }, status=status.HTTP_200_OK)
        return Response({
            'message': 'Pet was not in favorites.',
            'is_favorited': False
        }, status=status.HTTP_200_OK)


