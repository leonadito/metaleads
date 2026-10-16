from pathlib import Path
from decouple import config

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config('SECRET_KEY', default='dev-secret-key-change-in-production')
DEBUG = config('DEBUG', default=True, cast=bool)
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost,127.0.0.1').split(',')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'corsheaders',
    'leads',
    'crm',
    'django_tasks',
    'django_tasks_db',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'leadmanager.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'leads.tenant_switch.client_switcher',
            ],
        },
    },
]

WSGI_APPLICATION = 'leadmanager.wsgi.application'

# Postgres when DATABASE_URL has a value (beta: shared server, database
# metaleads_beta). Empty or missing keeps the SQLite file production uses
# (production's .env has an empty DATABASE_URL=).
if config('DATABASE_URL', default=''):
    import dj_database_url

    DATABASES = {
        'default': dj_database_url.parse(
            config('DATABASE_URL'), conn_max_age=config('CONN_MAX_AGE', default=60, cast=int)
        )
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'pt-br'
TIME_ZONE = 'America/Sao_Paulo'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Production serves /static/ from the host nginx (internal-static). The beta
# has no such route, so the app serves it with WhiteNoise.
if config('SERVE_STATIC', default=False, cast=bool):
    MIDDLEWARE.insert(1, 'whitenoise.middleware.WhiteNoiseMiddleware')

# Uploaded files (originals of the AI's documents). Every file of the apps
# lives in Cloudflare R2 (decision of 2026-10-16): with R2_BUCKET set, in the
# private bucket under the R2_PREFIX folder, served only through logged-in
# views; without it (tests, local), on disk.
MEDIA_ROOT = config('MEDIA_ROOT', default=str(BASE_DIR / 'media'))
MEDIA_URL = '/arquivos-internos/'  # never routed
R2_BUCKET = config('R2_BUCKET', default='')
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}
if R2_BUCKET:
    STORAGES['default'] = {
        'BACKEND': 'storages.backends.s3.S3Storage',
        'OPTIONS': {
            'bucket_name': R2_BUCKET,
            'endpoint_url': config('R2_ENDPOINT'),
            'access_key': config('R2_ACCESS_KEY_ID'),
            'secret_key': config('R2_SECRET_ACCESS_KEY'),
            'region_name': 'auto',
            'signature_version': 's3v4',
            'location': config('R2_PREFIX', default='metaleads'),
            'default_acl': None,
            'file_overwrite': False,
            'querystring_expire': 300,
        },
    }

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/dashboard/'
LOGOUT_REDIRECT_URL = '/login/'

# Single sign-on with the Agitare Core (leads/oidc.py). Off unless the client
# id is set, so production keeps its local login until the cutover. With it
# on, /login/ goes to the core; the Django admin keeps its password login.
OIDC_RP_CLIENT_ID = config('OIDC_RP_CLIENT_ID', default='')
OIDC_ENABLED = bool(OIDC_RP_CLIENT_ID)
if OIDC_ENABLED:
    INSTALLED_APPS.append('mozilla_django_oidc')
    AUTHENTICATION_BACKENDS = [
        'leads.oidc.CoreOIDCBackend',
        'django.contrib.auth.backends.ModelBackend',
    ]
    OIDC_RP_CLIENT_SECRET = config('OIDC_RP_CLIENT_SECRET')
    _OP = config('OIDC_OP_BASE_URL').rstrip('/')  # e.g. https://id.beta.agitare.com.br/o
    OIDC_OP_AUTHORIZATION_ENDPOINT = f'{_OP}/authorize/'
    OIDC_OP_TOKEN_ENDPOINT = f'{_OP}/token/'
    OIDC_OP_USER_ENDPOINT = f'{_OP}/userinfo/'
    OIDC_OP_JWKS_ENDPOINT = f'{_OP}/.well-known/jwks.json'
    OIDC_OP_LOGOUT_ENDPOINT = f'{_OP}/logout/'
    OIDC_OP_ISSUER = _OP  # checked on back-channel logout tokens
    OIDC_RP_SIGN_ALGO = 'RS256'
    OIDC_RP_SCOPES = 'openid email profile tenancy'
    OIDC_USE_PKCE = True
    OIDC_STORE_ID_TOKEN = True
    OIDC_OP_LOGOUT_URL_METHOD = 'leads.oidc.provider_logout'
    OIDC_POST_LOGOUT_REDIRECT_URI = f"{config('SITE_URL', default='https://metaleads.agitare.com.br').rstrip('/')}/login/?saiu=1"
    LOGIN_URL = 'oidc_authentication_init'
    LOGIN_REDIRECT_URL_FAILURE = '/login/?falhou=1'

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
}

CORS_ALLOWED_ORIGINS = []
CORS_ALLOW_CREDENTIALS = True

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
CSRF_TRUSTED_ORIGINS = config('SITE_URL', default='https://metaleads.agitare.com.br').rstrip('/').split(',')

TELEGRAM_BOT_TOKEN = config('TELEGRAM_BOT_TOKEN', default='')
TELEGRAM_BOT_NAME = config('TELEGRAM_BOT_NAME', default='')
SITE_URL = config('SITE_URL', default='https://metaleads.agitare.com.br')

GOOGLE_API_KEY = config('GOOGLE_API_KEY', default='')
GOOGLE_SERVICE_ACCOUNT_JSON = config('GOOGLE_SERVICE_ACCOUNT_JSON', default='')

# APScheduler — poll leads every 5 minutes
APSCHEDULER_DATETIME_FORMAT = "N j, Y, f:s a"
APSCHEDULER_RUN_NOW_TIMEOUT = 25

# Agitare Core: tenant copy (crm/core_sync.py). The secret is this app's
# webhook Endpoint secret in the core; it also authenticates the full sync.
CORE_API_URL = config('CORE_API_URL', default='')
CORE_WEBHOOK_SECRET = config('CORE_WEBHOOK_SECRET', default='')

# Where "/" goes: the old sheet dashboard until the cutover, the CRM kanban
# in the beta (docker-compose.beta.yml).
HOME_URL = config('HOME_URL', default='/dashboard/')
# Chatwoot, for the "open conversation" link on the lead page.
CHATWOOT_URL = config('CHATWOOT_URL', default='').rstrip('/')

# Nightly orphan check (crm/orphans.py): agipages' read-only integrity
# endpoint, and who gets the alert (e-mail to ADMINS; empty = log only).
AGIPAGES_API_URL = config('AGIPAGES_API_URL', default='')
AGIPAGES_INTEGRITY_TOKEN = config('AGIPAGES_INTEGRITY_TOKEN', default='')
ADMINS = [(e.strip(), e.strip()) for e in config('ALERT_EMAILS', default='').split(',') if e.strip()]

# Meta Lead Ads straight from the API + funnel back to Meta (crm/meta.py).
# Agitare's Meta app (webhook signature) and its system user (Graph API).
META_APP_SECRET = config('META_APP_SECRET', default='')
META_VERIFY_TOKEN = config('META_VERIFY_TOKEN', default='')
META_ACCESS_TOKEN = config('META_ACCESS_TOKEN', default='')
META_GRAPH_VERSION = config('META_GRAPH_VERSION', default='v24.0')
# Sending stages to Meta (Conversions API for CRM): off unless turned on, so
# the beta never feeds a client's real funnel. Each page also has its own
# switch (MetaPage.send_events).
META_CAPI_ENABLED = config('META_CAPI_ENABLED', default=False, cast=bool)
META_TEST_EVENT_CODE = config('META_TEST_EVENT_CODE', default='')
META_LEAD_EVENT_SOURCE = config('META_LEAD_EVENT_SOURCE', default='Agitare CRM')

# Task queue in the database (django-tasks + django-tasks-db), worked by
# `manage.py db_worker`, the container's second process. No Redis.
TASKS = {'default': {'BACKEND': 'django_tasks_db.DatabaseBackend'}}
