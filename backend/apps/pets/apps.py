from django.apps import AppConfig

class PetsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.pets'
    label = 'pets'

    def ready(self):
        import apps.pets.signals  # noqa: F401
