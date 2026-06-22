from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError


class Command(BaseCommand):
    help = "Create or update a GetOffline username/password login."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--password", required=True, help="Password for the user.")
        parser.add_argument("--admin", action="store_true", help="Grant staff and superuser flags.")
        parser.add_argument("--update", action="store_true", help="Update the password/flags when the user already exists.")

    def handle(self, *args, **options):
        username = options["username"].strip()
        password = options["password"]
        if not username:
            raise CommandError("username is required")
        if not password:
            raise CommandError("--password cannot be empty")
        User = get_user_model()
        user = User.objects.filter(username=username).first()
        if user is not None:
            if not options["update"]:
                raise CommandError(f"User {username!r} already exists. Use --update to change it.")
            user.set_password(password)
            user.is_staff = bool(options["admin"])
            user.is_superuser = bool(options["admin"])
            user.save(update_fields=["password", "is_staff", "is_superuser"])
            self.stdout.write(self.style.SUCCESS(f"Updated user {username}"))
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
        self.stdout.write(self.style.SUCCESS(f"Created user {username}"))
