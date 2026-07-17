"""Request logging helpers for production troubleshooting."""

from __future__ import annotations

import logging
from collections.abc import Callable

from django.core.exceptions import SuspiciousOperation
from django.http import HttpRequest, HttpResponse

log = logging.getLogger("app.requests")


class RequestDiagnosticsMiddleware:
    """Log request context for rejected or failing browser/API calls."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        try:
            response = self.get_response(request)
        except SuspiciousOperation:
            self._log_request(request, "suspicious request rejected", exc_info=True)
            raise
        except Exception:
            self._log_request(request, "request failed", exc_info=True)
            raise
        if response.status_code >= 400:
            self._log_request(
                request,
                "request returned error response",
                status_code=response.status_code,
            )
        return response

    def _log_request(
        self,
        request: HttpRequest,
        message: str,
        *,
        status_code: int | None = None,
        exc_info: bool = False,
    ) -> None:
        user = getattr(request, "user", None)
        username = (
            user.get_username() if getattr(user, "is_authenticated", False) else "-"
        )
        log.warning(
            "%s method=%s path=%s status=%s host=%s forwarded_host=%s origin=%s "
            "referer=%s remote_addr=%s user=%s content_type=%s",
            message,
            request.method,
            request.get_full_path(),
            status_code or "-",
            request.META.get("HTTP_HOST", ""),
            request.META.get("HTTP_X_FORWARDED_HOST", ""),
            request.META.get("HTTP_ORIGIN", ""),
            request.META.get("HTTP_REFERER", ""),
            request.META.get("REMOTE_ADDR", ""),
            username,
            request.META.get("CONTENT_TYPE", ""),
            exc_info=exc_info,
        )
