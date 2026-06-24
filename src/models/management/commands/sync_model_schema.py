from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Create missing shared-model tables and add missing columns for existing GetOffline databases."

    def handle(self, *args, **options):
        existing_tables = set(connection.introspection.table_names())
        models = list(apps.get_app_config("models").get_models())
        with connection.schema_editor() as schema_editor:
            for model in models:
                table_name = model._meta.db_table
                if table_name not in existing_tables:
                    schema_editor.create_model(model)
                    existing_tables.add(table_name)
                    self.stdout.write(self.style.SUCCESS(f"Created table {table_name}"))
                    continue

                existing_columns = {
                    column.name
                    for column in connection.introspection.get_table_description(
                        connection.cursor(), table_name
                    )
                }
                for field in model._meta.local_fields:
                    column_name = field.column
                    if column_name in existing_columns:
                        continue
                    schema_editor.add_field(model, field)
                    existing_columns.add(column_name)
                    self.stdout.write(
                        self.style.SUCCESS(f"Added column {table_name}.{column_name}")
                    )
