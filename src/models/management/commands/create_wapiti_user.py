"""Create a disposable account for an authenticated Wapiti scan."""

import secrets

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create a temporary username/password for an authenticated Wapiti scan."

    def handle(self, *args: object, **options: object) -> None:
        user_model = get_user_model()
        username = f"wapiti_scan_{secrets.token_hex(8)}"
        password = secrets.token_urlsafe(24)
        user_model.objects.create_user(username=username, password=password)

        # Keep this output machine-readable: the Make target passes the values
        # directly to Wapiti and never writes them to the repository.
        self.stdout.write(f"{username}:{password}")
