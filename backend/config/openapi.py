import re

def preprocessing_filter_spec_endpoints(endpoints):
    """
    OpenAPI 3.0 schema preprocessing hook for drf-spectacular.
    Keeps /api/v1/ canonical paths only, excluding legacy /api/... mirrors,
    catch-all /api/v1/{id}/... mirrors, and resolving operationId collisions.
    """
    filtered = []
    
    # Prefix list for flat legacy alias routes directly under /api/v1/
    legacy_shortcuts = (
        '/api/v1/shelter-applications/',
        '/api/v1/my-applications/',
        '/api/v1/apply/',
        '/api/v1/assign/',
        '/api/v1/driver-active/',
        '/api/v1/drivers/create/',
        '/api/v1/login/',
        '/api/v1/logout/',
        '/api/v1/register/',
        '/api/v1/tracking/',
        '/api/v1/me/',
        '/api/v1/notifications/',
        '/api/v1/token/',
        '/api/v1/public/pets/',
        '/api/v1/pets/public/pets/',
        '/api/v1/pets/shelter-pets/',
    )

    for endpoint in endpoints:
        path, path_regex, method, callback = endpoint
        
        # 1. Canonical /api/v1/ only (exclude /api/... legacy endpoints)
        if not path.startswith('/api/v1/'):
            continue
            
        # 2. Exclude catch-all /api/v1/{...}/ mirrors
        if re.match(r'^/api/v1/\{[^/]+\}/', path):
            continue
            
        # 3. Exclude redundant /api/v1/deliveries/ prefix mirrors (canonical is /api/v1/shelter/deliveries/, /api/v1/delivery/tasks/, /api/v1/payments/)
        if path.startswith('/api/v1/deliveries/'):
            continue
            
        # 4. Exclude redundant /api/v1/adoptions/ prefix mirrors (canonical is /api/v1/applications/, /api/v1/shelter/applications/, /api/v1/reviews/)
        if path.startswith('/api/v1/adoptions/'):
            continue
            
        # 5. Exclude /api/v1/admin/... (canonical is /api/v1/auth/admin/...)
        if path.startswith('/api/v1/admin/'):
            continue
            
        # 6. Exclude legacy shortcut routes at /api/v1/ root
        if any(path.startswith(prefix) for prefix in legacy_shortcuts):
            continue
            
        # 7. Drop str/int {id} variants in favor of canonical {public_id}
        if '{id}' in path and ('/shelter/deliveries/' in path or '/delivery/tasks/' in path):
            continue
        if '{pet_id}' in path:
            continue
            
        filtered.append(endpoint)
        
    return filtered
