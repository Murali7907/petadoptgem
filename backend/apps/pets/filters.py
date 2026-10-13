import django_filters
from .models import Pet

class PetFilter(django_filters.FilterSet):
    species = django_filters.CharFilter(field_name='species', lookup_expr='iexact')
    breed = django_filters.CharFilter(field_name='breed', lookup_expr='icontains')
    gender = django_filters.CharFilter(field_name='gender', lookup_expr='iexact')
    size = django_filters.CharFilter(field_name='size', lookup_expr='iexact')
    age = django_filters.NumberFilter(field_name='age_months')
    min_age = django_filters.NumberFilter(field_name='age_months', lookup_expr='gte')
    max_age = django_filters.NumberFilter(field_name='age_months', lookup_expr='lte')
    vaccinated = django_filters.BooleanFilter(field_name='is_vaccinated')
    is_vaccinated = django_filters.BooleanFilter(field_name='is_vaccinated')
    shelter = django_filters.CharFilter(field_name='shelter__organization_name', lookup_expr='icontains')
    shelter_id = django_filters.NumberFilter(field_name='shelter_id')
    city = django_filters.CharFilter(field_name='shelter__city', lookup_expr='icontains')
    status = django_filters.CharFilter(field_name='status', lookup_expr='iexact')

    class Meta:
        model = Pet
        fields = [
            'species',
            'breed',
            'gender',
            'size',
            'age',
            'min_age',
            'max_age',
            'vaccinated',
            'is_vaccinated',
            'shelter',
            'shelter_id',
            'city',
            'status',
        ]
