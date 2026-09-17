"""
Django settings for Sysvar Hub.

Backend local responsável pela operação da loja e
sincronização com a retaguarda Sysvar.
"""

from pathlib import Path
from decouple import Csv, config

from runtime.windows_runtime import ENV_FILE, load_env_file, local_hostnames_and_ips, merge_csv


BASE_DIR = Path(__file__).resolve().parent.parent

load_env_file(ENV_FILE)


# -----------------------------------------------------------------------------
# Segurança / ambiente
# -----------------------------------------------------------------------------

SECRET_KEY = config(
    "DJANGO_SECRET_KEY",
    default="dev-insecure-sysvarhub-change-me"
)

DEBUG = config(
    "DJANGO_DEBUG",
    cast=bool,
    default=True
)

if not DEBUG and SECRET_KEY == "dev-insecure-sysvarhub-change-me":
    raise RuntimeError(
        "DJANGO_SECRET_KEY deve ser configurado para executar com DJANGO_DEBUG=False."
    )

ALLOWED_HOSTS = merge_csv(
    config(
    "DJANGO_ALLOWED_HOSTS",
    cast=Csv(),
    default="127.0.0.1,localhost"
    ),
    local_hostnames_and_ips(),
)

CSRF_TRUSTED_ORIGINS = config(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    cast=Csv(),
    default=""
)


# -----------------------------------------------------------------------------
# Aplicativos
# -----------------------------------------------------------------------------

INSTALLED_APPS = [
    # Django
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    # Terceiros
    "corsheaders",
    "rest_framework",
    "rest_framework.authtoken",
    "django_filters",
    "drf_yasg",
    "django_extensions",

    # Apps do Sysvar Hub
    "core.apps.CoreConfig",
    "integracao.apps.IntegracaoConfig",
]


# -----------------------------------------------------------------------------
# Middleware
# -----------------------------------------------------------------------------

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]


ROOT_URLCONF = "sysvarhub.urls"


TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


WSGI_APPLICATION = "sysvarhub.wsgi.application"


# -----------------------------------------------------------------------------
# Banco de dados local da loja
# -----------------------------------------------------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": config("DB_NAME", default="sysvarhub_db"),
        "USER": config("DB_USER", default="root"),
        "PASSWORD": config("DB_PASSWORD", default=""),
        "HOST": config("DB_HOST", default="127.0.0.1"),
        "PORT": config("DB_PORT", default="3306"),
        "OPTIONS": {
            "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
        },
        "CONN_MAX_AGE": 300,
    }
}


# -----------------------------------------------------------------------------
# Validação de senha
# -----------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME":
        "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
    {
        "NAME":
        "django.contrib.auth.password_validation.MinimumLengthValidator"
    },
    {
        "NAME":
        "django.contrib.auth.password_validation.CommonPasswordValidator"
    },
    {
        "NAME":
        "django.contrib.auth.password_validation.NumericPasswordValidator"
    },
]


# -----------------------------------------------------------------------------
# Idioma / timezone
# -----------------------------------------------------------------------------

LANGUAGE_CODE = "pt-br"

TIME_ZONE = "America/Sao_Paulo"

USE_I18N = True

USE_TZ = True


# -----------------------------------------------------------------------------
# Arquivos estáticos
# -----------------------------------------------------------------------------

STATIC_URL = "static/"

STATIC_ROOT = BASE_DIR / "staticfiles"

STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

FRONTEND_DIST_DIR = Path(config("SYSVARHUB_FRONTEND_DIST_DIR", default=str(BASE_DIR / "frontend_dist")))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# -----------------------------------------------------------------------------
# CORS
# -----------------------------------------------------------------------------

if DEBUG:
    CORS_ALLOW_ALL_ORIGINS = True
else:
    CORS_ALLOW_ALL_ORIGINS = False
    CORS_ALLOWED_ORIGINS = config(
        "CORS_ALLOWED_ORIGINS",
        cast=Csv(),
        default=""
    )

CORS_ALLOW_CREDENTIALS = True


# -----------------------------------------------------------------------------
# Django REST Framework
# -----------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS":
        "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_THROTTLE_RATES": {
        "terminal_pareamento": "20/min",
        "operador_login": "10/min",
    },
}


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

SYSVARHUB_LOG_DIR = config("SYSVARHUB_LOG_DIR", default="")

LOG_HANDLERS = ["console"]
LOGGING_HANDLERS = {
    "console": {
        "class": "logging.StreamHandler",
        "formatter": "simple",
    },
}

if SYSVARHUB_LOG_DIR:
    Path(SYSVARHUB_LOG_DIR).mkdir(parents=True, exist_ok=True)
    LOGGING_HANDLERS["hub_file"] = {
        "class": "logging.handlers.RotatingFileHandler",
        "formatter": "simple",
        "filename": str(Path(SYSVARHUB_LOG_DIR) / "hub.log"),
        "maxBytes": 5 * 1024 * 1024,
        "backupCount": 5,
        "encoding": "utf-8",
    }
    LOG_HANDLERS.append("hub_file")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {
            "format": "[{levelname}] {message}",
            "style": "{",
        },
    },
    "handlers": LOGGING_HANDLERS,
    "root": {
        "handlers": LOG_HANDLERS,
        "level": "INFO",
    },
}
