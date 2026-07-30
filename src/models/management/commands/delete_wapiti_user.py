"""Delete a disposable account and any profile data created during a scan."""

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Delete a temporary account created for an authenticated Wapiti scan."

    def add_arguments(self, parser):
        parser.add_argument("username")

    @transaction.atomic
    def handle(self, *args: object, **options: object) -> None:
        username = str(options["username"])
        if not username.startswith("wapiti_scan_"):
            raise CommandError("Only wapiti_scan_ users can be deleted by this command")

        # Profile-scoped records intentionally use a string profile_id rather
        # than a foreign key, so remove those records before removing the user.
        for model in apps.get_models():
            if not any(field.name == "profile_id" for field in model._meta.fields):
                continue
            model.objects.filter(profile_id=username).delete()

        get_user_model().objects.filter(username=username).delete()
