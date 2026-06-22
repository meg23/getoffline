from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError
from django.utils import timezone

from models.models import ProfileConfigValue


class Command(BaseCommand):
    help = "Create or update a GetOffline username/password login."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--password", required=True, help="Password for the user.")
        parser.add_argument("--admin", action="store_true", help="Grant staff and superuser flags.")
        parser.add_argument("--update", action="store_true", help="Update the password/flags when the user already exists.")
        parser.add_argument("--downloads-root", default="./downloads", help="Root directory for per-user download folders.")

    def handle(self, *args, **options):
        username = options["username"].strip()
        password = options["password"]
        if not username:
            raise CommandError("username is required")
        if not password:
            raise CommandError("--password cannot be empty")
        downloads_root = Path(str(options["downloads_root"])).expanduser()
        output_root = downloads_root / username

        User = get_user_model()
        user = User.objects.filter(username=username).first()
        if user is not None:
            if not options["update"]:
                raise CommandError(f"User {username!r} already exists. Use --update to change it.")
            user.set_password(password)
            user.is_staff = bool(options["admin"])
            user.is_superuser = bool(options["admin"])
            user.save(update_fields=["password", "is_staff", "is_superuser"])
            self._ensure_downloads_directory(username, output_root)
            self.stdout.write(self.style.SUCCESS(f"Updated user {username}; downloads directory: {output_root}"))
            return
        try:
            User.objects.create_user(
                username=username,
                password=password,
                is_staff=bool(options["admin"]),
                is_superuser=bool(options["admin"]),
            )
        except IntegrityError as exc:
            raise CommandError(str(exc)) from exc
        self._ensure_downloads_directory(username, output_root)
        self.stdout.write(self.style.SUCCESS(f"Created user {username}; downloads directory: {output_root}"))

    def _ensure_downloads_directory(self, username: str, output_root: Path) -> None:
        output_root.mkdir(parents=True, exist_ok=True)
        ProfileConfigValue.objects.update_or_create(
            profile_id=username,
            key="output_root",
            defaults={"value": str(output_root), "updated_at": timezone.now()},
        )
