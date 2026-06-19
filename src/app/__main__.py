import os
import sys

from django.core.management import execute_from_command_line


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings")
    execute_from_command_line([sys.argv[0], "runserver", os.getenv("GETOFFLINE_APP_ADDR", "127.0.0.1:8080")])


if __name__ == "__main__":
    main()
