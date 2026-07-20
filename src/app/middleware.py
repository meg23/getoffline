"""Request middleware for local-network deployments."""

from __future__ import annotations

import ipaddress

from django.conf import settings
from django.http.request import split_domain_port


class AllowPrivateNetworkHostMiddleware:
    """Trust private/link-local IP Host headers for self-hosted LAN access.

    Docker Compose deployments are often opened from phones or other machines via
    the host's LAN IP address. Users may also have an existing
    GETOFFLINE_DJANGO_ALLOWED_HOSTS value from older releases, so changing the
    default alone is not enough. Django validates Host lazily through
    request.get_host(); adding private IP hosts before later middleware/views run
    prevents legitimate local-network requests from becoming Bad Request (400).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        host = request.META.get("HTTP_X_FORWARDED_HOST") or request.META.get(
            "HTTP_HOST", ""
        )
        domain, _port = split_domain_port(host)
        if domain:
            try:
                address = ipaddress.ip_address(domain.strip("[]"))
            except ValueError:
                address = None
            if address and (
                address.is_private or address.is_loopback or address.is_link_local
            ):
                allowed_hosts = list(getattr(settings, "ALLOWED_HOSTS", []))
                if "*" not in allowed_hosts and domain not in allowed_hosts:
                    allowed_hosts.append(domain)
                    settings.ALLOWED_HOSTS = allowed_hosts
        return self.get_response(request)
