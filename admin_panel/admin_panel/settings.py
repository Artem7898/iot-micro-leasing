"""Django back office settings for the shared IoT application database."""

import os
from collections.abc import Mapping
from math import isfinite
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = BASE_DIR.parent
load_dotenv(REPOSITORY_ROOT / ".env", override=False)


def env_bool(name: str, default: bool = False, values: Mapping[str, str] | None = None) -> bool:
    value = (os.environ if values is None else values).get(name)
    if value is None:
        return default
    if value.lower() in {"true", "1", "yes", "on"}:
        return True
    if value.lower() in {"false", "0", "no", "off"}:
        return False
    raise ImproperlyConfigured(f"{name} must be a boolean")


def security_config(values: Mapping[str, str] | None = None) -> dict:
    """Reject accidental development settings in a production environment."""
    values = os.environ if values is None else values
    environment = values.get("ENVIRONMENT", "development")
    if environment not in {"development", "test", "production"}:
        raise ImproperlyConfigured("ENVIRONMENT must be development, test, or production")
    debug = env_bool("DJANGO_DEBUG", environment != "production", values)
    if environment == "production" and debug:
        raise ImproperlyConfigured("DJANGO_DEBUG must be false in production")
    secure_mode = not debug
    secret_key = values.get("DJANGO_SECRET_KEY", "development-only-change-before-deployment")
    if secure_mode and (
        len(secret_key) < 50
        or len(set(secret_key)) < 5
        or secret_key.startswith("django-insecure-")
        or secret_key == "development-only-change-before-deployment"
    ):
        raise ImproperlyConfigured(
            "Set a strong DJANGO_SECRET_KEY of at least 50 characters when debug is disabled"
        )
    hosts = [
        host.strip()
        for host in values.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",")
        if host.strip()
    ]
    if secure_mode and (
        "DJANGO_ALLOWED_HOSTS" not in values or not hosts or any("*" in host for host in hosts)
    ):
        raise ImproperlyConfigured("Set explicit DJANGO_ALLOWED_HOSTS when debug is disabled")
    flags = {}
    for name in ("SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE", "SECURE_SSL_REDIRECT"):
        flags[name] = env_bool(f"DJANGO_{name}", secure_mode, values)
        if secure_mode and not flags[name]:
            raise ImproperlyConfigured(f"DJANGO_{name} must be true when debug is disabled")
    return {
        "ENVIRONMENT": environment,
        "DEBUG": debug,
        "SECRET_KEY": secret_key,
        "ALLOWED_HOSTS": hosts,
        **flags,
    }


def database_config(url: str) -> dict:
    """Accept the same sync/async SQLAlchemy database URLs as the API."""
    parsed = urlsplit(url)
    scheme = parsed.scheme.split("+", 1)[0]
    if scheme == "sqlite":
        if parsed.netloc or not parsed.path:
            raise ImproperlyConfigured("DATABASE_URL must name a SQLite database file")
        # SQLAlchemy uses three slashes for relative files, four for absolute files.
        filename = unquote(parsed.path[1:])
        if filename == ":memory:":
            name = filename
        else:
            path = Path(filename)
            name = path if path.is_absolute() else REPOSITORY_ROOT / path
        query = dict(parse_qsl(parsed.query))
        if set(query) - {"timeout"}:
            raise ImproperlyConfigured("Unsupported SQLite DATABASE_URL options")
        return {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": name,
            "OPTIONS": {"timeout": float(query.get("timeout", "10"))},
        }
    if scheme in {"postgres", "postgresql"}:
        options = dict(parse_qsl(parsed.query))
        if set(options) - {
            "sslmode",
            "sslrootcert",
            "sslcert",
            "sslkey",
            "application_name",
            "options",
            "target_session_attrs",
            "connect_timeout",
        }:
            raise ImproperlyConfigured("Unsupported PostgreSQL DATABASE_URL options")
        try:
            connect_timeout = int(
                options.get("connect_timeout", os.environ.get("DB_CONNECT_TIMEOUT", "5"))
            )
        except ValueError as exc:
            raise ImproperlyConfigured(
                "PostgreSQL connect_timeout must be an integer between 1 and 30"
            ) from exc
        if not 1 <= connect_timeout <= 30:
            raise ImproperlyConfigured(
                "PostgreSQL connect_timeout must be between 1 and 30 seconds"
            )
        options["connect_timeout"] = connect_timeout
        return {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": unquote(parsed.path.lstrip("/")),
            "USER": unquote(parsed.username or ""),
            "PASSWORD": unquote(parsed.password or ""),
            "HOST": parsed.hostname or "localhost",
            "PORT": str(parsed.port or 5432),
            "CONN_MAX_AGE": int(os.environ.get("DJANGO_DB_CONN_MAX_AGE", "0")),
            "CONN_HEALTH_CHECKS": True,
            "OPTIONS": options,
        }
    raise ImproperlyConfigured("DATABASE_URL must use SQLite or PostgreSQL")


SECURITY = security_config()
ENVIRONMENT = SECURITY["ENVIRONMENT"]
DEBUG = SECURITY["DEBUG"]
SECRET_KEY = SECURITY["SECRET_KEY"]
ALLOWED_HOSTS = SECURITY["ALLOWED_HOSTS"]
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

STATE_DIR = Path(os.environ.get("IOT_STATE_DIR", REPOSITORY_ROOT / ".local" / "state"))
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{STATE_DIR / 'dev.sqlite3'}")
DATABASES = {"default": database_config(DATABASE_URL)}

REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
REDIS_CONNECT_TIMEOUT = float(os.environ.get("REDIS_CONNECT_TIMEOUT", "1"))
REDIS_SOCKET_TIMEOUT = float(os.environ.get("REDIS_SOCKET_TIMEOUT", "1"))
REDIS_MAX_CONNECTIONS = int(os.environ.get("REDIS_MAX_CONNECTIONS", "8"))
if (
    not (isfinite(REDIS_CONNECT_TIMEOUT) and isfinite(REDIS_SOCKET_TIMEOUT))
    or REDIS_CONNECT_TIMEOUT <= 0
    or REDIS_SOCKET_TIMEOUT <= 0
    or REDIS_MAX_CONNECTIONS <= 0
):
    raise ImproperlyConfigured(
        "Redis timeouts must be finite and positive, and pool size must be positive"
    )

INSTALLED_APPS = [
    "unfold",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "devices",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "admin_panel.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
WSGI_APPLICATION = "admin_panel.wsgi.application"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = STATE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = SECURITY["SESSION_COOKIE_SECURE"]
CSRF_COOKIE_SECURE = SECURITY["CSRF_COOKIE_SECURE"]
SECURE_SSL_REDIRECT = SECURITY["SECURE_SSL_REDIRECT"]
SECURE_HSTS_SECONDS = int(
    os.environ.get("DJANGO_SECURE_HSTS_SECONDS", "0" if DEBUG else "31536000")
)
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = False
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

UNFOLD = {"SITE_TITLE": "IoT Leasing", "SITE_HEADER": "IoT Leasing"}
