from rest_framework.throttling import SimpleRateThrottle


class PetsGeoRateThrottle(SimpleRateThrottle):
    """
    Rate throttle for pet discovery endpoints supplying geolocation coordinates.
    Protects shelter physical locations from automated trilateration scraping.
    Env configurable via PETS_GEO_THROTTLE_RATE (defaults to 30/min).
    Only applies when request query params carry client-supplied coordinates (lat/lng).
    """
    scope = 'pets_geo'

    def get_cache_key(self, request, view):
        lat = request.query_params.get('lat')
        lng = request.query_params.get('lng')
        # Only throttle requests carrying client-supplied coordinates
        if lat is None or lng is None:
            return None

        if request.user and request.user.is_authenticated:
            ident = str(request.user.pk)
        else:
            ident = self.get_ident(request)

        return self.cache_format % {
            'scope': self.scope,
            'ident': ident
        }

    def get_rate(self):
        import os
        env_val = os.getenv('PETS_GEO_THROTTLE_RATE')
        if env_val:
            return env_val
        return super().get_rate()
