import os
from typing import Any
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
USE_IN_MEMORY_TEST_DB = os.getenv(
    "GETOFFLINE_TEST_IN_MEMORY_DB", "0"
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
API_RUNTIME = os.getenv("GETOFFLINE_DJANGO_ROLE", "frontend").strip().lower() in {
    "api",
    "worker",
}
USE_DATABASE_AUTH = USE_IN_MEMORY_TEST_DB or API_RUNTIME

if USE_DATABASE_AUTH:
    import pymysql

    pymysql.install_as_MySQLdb()

SECRET_KEY = os.getenv(
    "GETOFFLINE_DJANGO_SECRET_KEY",
    "getoffline-dev-secret",
)
DEBUG = os.getenv("GETOFFLINE_DJANGO_DEBUG", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _csv_env(name: str, default: str) -> list[str]:
    return [
        value.strip() for value in os.getenv(name, default).split(",") if value.strip()
    ]


def _allowed_hosts() -> list[str]:
    # The Docker/nginx frontend is commonly reached from another device on the
    # local network (for example http://192.168.x.x:8080). Django validates the
    # Host header before routing requests, so the default accepts those LAN
    # addresses. Always include Docker/internal service names too: users may
    # carry forward an older strict GETOFFLINE_DJANGO_ALLOWED_HOSTS value, and
    # the split frontend proxies /settings/ and /batch-update/ through the API
    # service using the internal ``api`` hostname.
    configured = _csv_env("GETOFFLINE_DJANGO_ALLOWED_HOSTS", "*")
    internal = _csv_env(
        "GETOFFLINE_DJANGO_INTERNAL_ALLOWED_HOSTS",
        "localhost,127.0.0.1,testserver,frontend,api",
    )
    return list(dict.fromkeys([*configured, *internal]))


ALLOWED_HOSTS = _allowed_hosts()


STRICT_ALLOWED_HOSTS = os.getenv(
    "GETOFFLINE_DJANGO_STRICT_ALLOWED_HOSTS", "0"
).strip().lower() in {"1", "true", "yes", "on"}
if not STRICT_ALLOWED_HOSTS and "*" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append("*")

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "GETOFFLINE_CSRF_TRUSTED_ORIGINS",
        "http://localhost,http://localhost:8080,http://127.0.0.1,http://127.0.0.1:8080",
    ).split(",")
    if origin.strip()
]
CSRF_COOKIE_SECURE = os.getenv(
    "GETOFFLINE_CSRF_COOKIE_SECURE", "0"
).strip().lower() in {"1", "true", "yes", "on"}
SESSION_COOKIE_SECURE = os.getenv(
    "GETOFFLINE_SESSION_COOKIE_SECURE", "0"
).strip().lower() in {"1", "true", "yes", "on"}
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_HTTPONLY = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
ROOT_URLCONF = "frontend.urls"
STATIC_URL = "/static/"
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "library"
LOGOUT_REDIRECT_URL = "/login/"
STATIC_ROOT = Path(os.getenv("GETOFFLINE_STATIC_ROOT", "/app/staticfiles"))
STATIC_MANIFEST_ENABLED = os.getenv(
    "GETOFFLINE_STATIC_MANIFEST", "0"
).strip().lower() in {"1", "true", "yes", "on"}
if STATIC_MANIFEST_ENABLED:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"
        },
    }
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True

INSTALLED_APPS = ["django.contrib.staticfiles", "frontend"]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "frontend.middleware.AllowPrivateNetworkHostMiddleware",
    "frontend.middleware.SecurityHeadersMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
]
if USE_DATABASE_AUTH:
    INSTALLED_APPS = [
        "django.contrib.auth",
        "django.contrib.contenttypes",
        "django.contrib.sessions",
        "django.contrib.staticfiles",
        "models.apps.SharedModelsConfig",
        "frontend",
    ]
    MIDDLEWARE[4:4] = [
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
    ]
TEMPLATE_CONTEXT_PROCESSORS = [
    "django.template.context_processors.request",
]
if USE_DATABASE_AUTH:
    TEMPLATE_CONTEXT_PROCESSORS.append("django.contrib.auth.context_processors.auth")

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": TEMPLATE_CONTEXT_PROCESSORS},
    }
]

DATABASES: dict[str, dict[str, Any]]
if USE_IN_MEMORY_TEST_DB:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }
elif API_RUNTIME:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": os.getenv("GETOFFLINE_DB_NAME", "getoffline"),
            "USER": os.getenv("GETOFFLINE_DB_USER", "getoffline"),
            "PASSWORD": os.getenv("GETOFFLINE_DB_PASSWORD", ""),
            "HOST": os.getenv("GETOFFLINE_DB_HOST", "127.0.0.1"),
            "PORT": os.getenv("GETOFFLINE_DB_PORT", "3306"),
            "OPTIONS": {"charset": "utf8mb4"},
        }
    }
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.dummy"}}
RABBITMQ_URL = os.getenv(
    "GETOFFLINE_RABBITMQ_URL", "amqp://guest:guest@127.0.0.1:5672/%2F"
)
RABBITMQ_EXCHANGE = os.getenv("GETOFFLINE_RABBITMQ_EXCHANGE", "getoffline")
CPU_SCHEDULER_SLOTS = int(os.getenv("GETOFFLINE_CPU_SCHEDULER_SLOTS", "3"))
