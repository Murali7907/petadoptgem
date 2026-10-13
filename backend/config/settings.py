import os
from datetime import timedelta
from pathlib import Path
from dotenv import load_dotenv
from django.core.exceptions import ImproperlyConfigured

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from .env file
load_dotenv(BASE_DIR / '.env')

# Cast DEBUG cleanly to boolean, defaulting to False (fail closed in production)
DEBUG = os.getenv('DEBUG', 'False').strip().lower() in ('true', '1', 't', 'yes', 'y')

# SECRET_KEY (must fail closed when DEBUG is False)
SECRET_KEY = os.getenv('SECRET_KEY', '').strip()
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = 'django-insecure-pet-adoption-secret-key-dev-only'
    else:
        raise ImproperlyConfigured("SECRET_KEY environment variable is required and cannot be empty when DEBUG is False.")

# ALLOWED_HOSTS from env (no '*' when DEBUG is False; localhost/127.0.0.1 defaults only when DEBUG is True)
ALLOWED_HOSTS_ENV = os.getenv('ALLOWED_HOSTS', '').strip()
if ALLOWED_HOSTS_ENV:
    ALLOWED_HOSTS = [host.strip() for host in ALLOWED_HOSTS_ENV.split(',') if host.strip()]
    if not DEBUG and '*' in ALLOWED_HOSTS:
        raise ImproperlyConfigured("Wildcard '*' in ALLOWED_HOSTS is forbidden when DEBUG is False.")
else:
    if DEBUG:
        ALLOWED_HOSTS = ['localhost', '127.0.0.1']
    else:
        raise ImproperlyConfigured("ALLOWED_HOSTS environment variable is required when DEBUG is False.")

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Third-party apps
    'corsheaders',
    'cloudinary_storage',
    'cloudinary',
    'rest_framework',
    'rest_framework_simplejwt.token_blacklist',
    'django_filters',
    'drf_spectacular',
    # Modular local apps
    'apps.accounts',
    'apps.pets',
    'apps.adoptions',
    'apps.logistics',
    'apps.reviews',
]

AUTH_USER_MODEL = 'accounts.User'

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# PostgreSQL Database Configuration using dotenv credentials
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.getenv('DB_NAME', 'pet_adoption'),
        'USER': os.getenv('DB_USER', 'postgres'),
        'PASSWORD': os.getenv('DB_PASSWORD', 'postgres'),
        'HOST': os.getenv('DB_HOST', '127.0.0.1'),
        'PORT': os.getenv('DB_PORT', '5432'),
    }
}

# Delivery OTP Security Configuration
OTP_TTL_MINUTES = int(os.getenv('OTP_TTL_MINUTES', '1440'))
OTP_MAX_ATTEMPTS = int(os.getenv('OTP_MAX_ATTEMPTS', '3'))
OTP_THROTTLE_RATE = os.getenv('OTP_THROTTLE_RATE', '10/min')

# Django REST Framework Settings
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_FILTER_BACKENDS': (
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ),
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 12,
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.ScopedRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'otp_verify': OTP_THROTTLE_RATE,
    },
}

# DRF Spectacular OpenAPI 3.0 Settings
SPECTACULAR_SETTINGS = {
    'TITLE': 'KindHeart Pet Adoption & Logistics API',
    'DESCRIPTION': 'Public Pet Discovery, Shelter Pet Management, Adoption Workflow, Real-Time Fleet Delivery & Tracking, and Razorpay Payments.',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'COMPONENT_SPLIT_REQUEST': True,
    'SECURITY': [{'jwt_auth': []}],
    'SECURITY_SCHEMES': {
        'jwt_auth': {
            'type': 'http',
            'scheme': 'bearer',
            'bearerFormat': 'JWT',
        }
    },
}

# Simple JWT Configuration
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=60),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'AUTH_HEADER_TYPES': ('Bearer',),
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Static & Media files
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Cloudinary CDN & Media Storage Configuration
USE_CLOUDINARY = os.getenv('USE_CLOUDINARY', 'False').lower() in ('true', '1', 't')

CLOUDINARY_CLOUD_NAME = os.getenv('CLOUDINARY_CLOUD_NAME', '').strip()
CLOUDINARY_API_KEY = os.getenv('CLOUDINARY_API_KEY', '').strip()
CLOUDINARY_API_SECRET = os.getenv('CLOUDINARY_API_SECRET', '').strip()

if USE_CLOUDINARY:
    missing_creds = [
        name for name, val in [
            ('CLOUDINARY_CLOUD_NAME', CLOUDINARY_CLOUD_NAME),
            ('CLOUDINARY_API_KEY', CLOUDINARY_API_KEY),
            ('CLOUDINARY_API_SECRET', CLOUDINARY_API_SECRET),
        ] if not val
    ]
    if missing_creds:
        from django.core.exceptions import ImproperlyConfigured
        raise ImproperlyConfigured(f"USE_CLOUDINARY is True, but required credentials are missing or empty: {', '.join(missing_creds)}")

CLOUDINARY_STORAGE = {
    'CLOUD_NAME': CLOUDINARY_CLOUD_NAME or 'mock_cloud',
    'API_KEY': CLOUDINARY_API_KEY or 'mock_key',
    'API_SECRET': CLOUDINARY_API_SECRET or 'mock_secret',
}

if USE_CLOUDINARY:
    DEFAULT_FILE_STORAGE = 'cloudinary_storage.storage.MediaCloudinaryStorage'
    STORAGES = {
        'default': {
            'BACKEND': 'cloudinary_storage.storage.MediaCloudinaryStorage',
        },
        'raw': {
            'BACKEND': 'cloudinary_storage.storage.RawMediaCloudinaryStorage',
        },
        'staticfiles': {
            'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
        },
    }
else:
    DEFAULT_FILE_STORAGE = 'django.core.files.storage.FileSystemStorage'
    STORAGES = {
        'default': {
            'BACKEND': 'django.core.files.storage.FileSystemStorage',
        },
        'raw': {
            'BACKEND': 'django.core.files.storage.FileSystemStorage',
        },
        'staticfiles': {
            'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
        },
    }

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Email Configuration
EMAIL_BACKEND = os.getenv('EMAIL_BACKEND', 'django.core.mail.backends.console.EmailBackend')
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'KindHeart Pet Adoption <no-reply@kindheart.org>')
SERVER_EMAIL = DEFAULT_FROM_EMAIL

# Razorpay Payment Gateway Configuration (fail-closed in production)
RAZORPAY_KEY_ID = os.getenv('RAZORPAY_KEY_ID', '').strip()
RAZORPAY_KEY_SECRET = os.getenv('RAZORPAY_KEY_SECRET', '').strip()
RAZORPAY_WEBHOOK_SECRET = os.getenv('RAZORPAY_WEBHOOK_SECRET', '').strip()

if not DEBUG:
    missing_razorpay = [
        name for name, val in [
            ('RAZORPAY_KEY_ID', RAZORPAY_KEY_ID),
            ('RAZORPAY_KEY_SECRET', RAZORPAY_KEY_SECRET),
            ('RAZORPAY_WEBHOOK_SECRET', RAZORPAY_WEBHOOK_SECRET),
        ] if not val
    ]
    if missing_razorpay:
        raise ImproperlyConfigured(
            f"Missing required Razorpay credentials when DEBUG is False: {', '.join(missing_razorpay)}"
        )
else:
    RAZORPAY_KEY_ID = RAZORPAY_KEY_ID or 'rzp_test_mock_key_id'
    RAZORPAY_KEY_SECRET = RAZORPAY_KEY_SECRET or 'mock_secret_key_12345'
    RAZORPAY_WEBHOOK_SECRET = RAZORPAY_WEBHOOK_SECRET or 'mock_webhook_secret_12345'

# Cross-Origin Resource Sharing (CORS) & CSRF Configuration
# Note: Authentication across the API is stateless SimpleJWT (Bearer tokens in Authorization headers),
# so browser session CSRF tokens do not apply to DRF API endpoints. CSRF_TRUSTED_ORIGINS is configured
# for Django Admin and browser-based interfaces.
cors_origins_env = os.getenv('CORS_ALLOWED_ORIGINS', '').strip()
if cors_origins_env:
    CORS_ALLOWED_ORIGINS = [origin.strip() for origin in cors_origins_env.split(',') if origin.strip()]
else:
    CORS_ALLOWED_ORIGINS = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_HEADERS = [
    'accept',
    'accept-encoding',
    'authorization',
    'content-type',
    'dnt',
    'origin',
    'user-agent',
    'x-csrftoken',
    'x-requested-with',
]

csrf_trusted_env = os.getenv('CSRF_TRUSTED_ORIGINS', '').strip()
if csrf_trusted_env:
    CSRF_TRUSTED_ORIGINS = [origin.strip() for origin in csrf_trusted_env.split(',') if origin.strip()]
else:
    CSRF_TRUSTED_ORIGINS = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
