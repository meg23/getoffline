import os
from pathlib import Path

import pymysql

pymysql.install_as_MySQLdb()

BASE_DIR = Path(__file__).resolve().parent
SECRET_KEY = os.getenv("GETOFFLINE_DJANGO_SECRET_KEY", "getoffline-dev-secret")
DEBUG = os.getenv("GETOFFLINE_DJANGO_DEBUG", "0").strip().lower() in {"1", "true", "yes", "on"}
ALLOWED_HOSTS = [host.strip() for host in os.getenv("GETOFFLINE_DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if host.strip()]
ROOT_URLCONF = "app.urls"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "models.apps.SharedModelsConfig",
    "app",
]
MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
    }
]
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
RABBITMQ_URL = os.getenv("GETOFFLINE_RABBITMQ_URL", "amqp://guest:guest@127.0.0.1:5672/%2F")
RABBITMQ_EXCHANGE = os.getenv("GETOFFLINE_RABBITMQ_EXCHANGE", "getoffline")
