"""Authentication/profile helpers decoupled from templates."""

from __future__ import annotations

from django.http import HttpRequest


def profile_id_for_request(request: HttpRequest) -> str:
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return str(user.get_username() or "default")
    return "default"
