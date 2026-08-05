"""Authentication helpers for the JSON API."""

from __future__ import annotations

import base64
import binascii
import secrets
from collections.abc import Callable
from functools import wraps
from typing import TypeVar, cast

from django.conf import settings
from django.contrib.auth import authenticate
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import CsrfViewMiddleware
from django.views.decorators.csrf import csrf_exempt

ViewFunc = TypeVar("ViewFunc", bound=Callable[..., HttpResponse])
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
_CSRF_CHECKER = CsrfViewMiddleware(lambda request: HttpResponse())


def _csrf_checked_view(request: HttpRequest) -> HttpResponse:
    return HttpResponse()


def _json_unauthorized() -> JsonResponse:
    response = JsonResponse(
        {"ok": False, "error": "Authentication credentials were not provided"},
        status=401,
    )
    response["WWW-Authenticate"] = 'Basic realm="Get Offline API"'
    return response


def _authenticate_basic(request: HttpRequest) -> bool:
    header = request.META.get("HTTP_AUTHORIZATION", "")
    if not header.lower().startswith("basic "):
        return False
    encoded = header.split(" ", 1)[1].strip()
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    username, separator, password = decoded.partition(":")
    if not separator:
        return False
    user = authenticate(request, username=username, password=password)
    if user is None or not user.is_active:
        return False
    request.user = user
    return True


def _csrf_failure_response(
    request: HttpRequest, view_func: Callable[..., HttpResponse]
) -> HttpResponse | None:
    return _CSRF_CHECKER.process_view(request, view_func, (), {})


def api_login_required(view_func: ViewFunc) -> ViewFunc:
    """Require API authentication while supporting Basic auth without CSRF.

    Browser/session clients must still satisfy Django CSRF checks for unsafe
    methods. Non-browser clients can instead use HTTP Basic authentication and
    do not need a CSRF token because credentials are supplied explicitly on the
    request rather than ambient browser cookies.
    """

    @wraps(view_func)
    def wrapper(request: HttpRequest, *args: object, **kwargs: object) -> HttpResponse:
        authenticated_with_basic = _authenticate_basic(request)
        if not authenticated_with_basic and not request.user.is_authenticated:
            return _json_unauthorized()
        if (
            not authenticated_with_basic
            and request.method.upper() not in SAFE_METHODS
            and _csrf_failure_response(request, _csrf_checked_view) is not None
        ):
            return JsonResponse(
                {"ok": False, "error": "CSRF token missing or incorrect"},
                status=403,
            )
        return view_func(request, *args, **kwargs)

    return cast(ViewFunc, csrf_exempt(wrapper))


def worker_api_login_required(view_func: ViewFunc) -> ViewFunc:
    """Authenticate internal worker media requests with a shared secret."""

    @wraps(view_func)
    def wrapper(request: HttpRequest, *args: object, **kwargs: object) -> HttpResponse:
        configured_token = str(getattr(settings, "WORKER_API_TOKEN", "") or "")
        supplied_token = str(request.headers.get("X-GetOffline-Worker-Token", ""))
        if not configured_token or not secrets.compare_digest(
            supplied_token, configured_token
        ):
            return _json_unauthorized()
        return view_func(request, *args, **kwargs)

    return cast(ViewFunc, wrapper)
