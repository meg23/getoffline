import os
from pathlib import Path

import pymysql

pymysql.install_as_MySQLdb()

BASE_DIR = Path(__file__).resolve().parent
SECRET_KEY = os.getenv("GETOFFLINE_DJANGO_SECRET_KEY", "getoffline-dev-secret")
DEBUG = os.getenv("GETOFFLINE_DJANGO_DEBUG", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv(
        "GETOFFLINE_DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost"
    ).split(",")
    if host.strip()
]

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
if os.getenv("GETOFFLINE_DB_ENGINE", "mysql").strip().lower() == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.getenv("GETOFFLINE_DB_NAME", ":memory:"),
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
