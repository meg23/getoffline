import os
from typing import Any
from pathlib import Path

import pymysql

pymysql.install_as_MySQLdb()

BASE_DIR = Path(__file__).resolve().parent
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
ROOT_URLCONF = "app.urls"
STATIC_URL = "/static/"
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "library"
LOGOUT_REDIRECT_URL = "/login/"
STATIC_ROOT = Path(os.getenv("GETOFFLINE_STATIC_ROOT", "/app/staticfiles"))
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "models.apps.SharedModelsConfig",
    "app",
]
MIDDLEWARE = [
    "app.middleware.AllowPrivateNetworkHostMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
            ]
        },
    }
]
USE_IN_MEMORY_TEST_DB = os.getenv(
    "GETOFFLINE_TEST_IN_MEMORY_DB", "0"
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

DATABASES: dict[str, dict[str, Any]]
if USE_IN_MEMORY_TEST_DB:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }
else:
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
RABBITMQ_URL = os.getenv(
    "GETOFFLINE_RABBITMQ_URL", "amqp://guest:guest@127.0.0.1:5672/%2F"
)
RABBITMQ_EXCHANGE = os.getenv("GETOFFLINE_RABBITMQ_EXCHANGE", "getoffline")
CPU_SCHEDULER_SLOTS = int(os.getenv("GETOFFLINE_CPU_SCHEDULER_SLOTS", "3"))
