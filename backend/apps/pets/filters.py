import django_filters
from .models import Pet

class PetFilter(django_filters.FilterSet):
    species = django_filters.CharFilter(field_name='species', lookup_expr='iexact')
    breed = django_filters.CharFilter(field_name='breed', lookup_expr='icontains')
    gender = django_filters.CharFilter(field_name='gender', lookup_expr='iexact')
    size = django_filters.CharFilter(field_name='size', lookup_expr='iexact')
    min_age = django_filters.NumberFilter(field_name='age_months', lookup_expr='gte')
    max_age = django_filters.NumberFilter(field_name='age_months', lookup_expr='lte')
    city = django_filters.CharFilter(field_name='shelter__city', lookup_expr='icontains')

    class Meta:
        model = Pet
        fields = [
            'species',
            'breed',
            'gender',
            'size',
            'min_age',
            'max_age',
            'city',
        ]
