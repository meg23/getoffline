#!/usr/bin/env python
import os
import sys
import django
from pathlib import Path

os.environ['GETOFFLINE_DJANGO_ROLE'] = 'api'
os.environ['DJANGO_SETTINGS_MODULE'] = 'frontend.settings'
django.setup()

from django.template.loader import get_template
from django.template.exceptions import TemplateSyntaxError

# List all templates to check
templates_to_check = [
    'registration/login.html',
    'app/library.html',
    'app/jobs.html',
    'app/player.html',
    'app/settings.html',
]

print("=" * 60)
print("DJANGO TEMPLATE VALIDATION")
print("=" * 60)

errors_found = False

for template_name in templates_to_check:
    try:
        template = get_template(template_name)
        print(f"✓ {template_name:40} - OK")
    except TemplateSyntaxError as e:
        errors_found = True
        print(f"✗ {template_name:40} - ERROR")
        print(f"  Line {e.lineno}: {e.msg}")
        if hasattr(e, 'source') and e.source:
            print(f"  File: {e.source[0] if isinstance(e.source, tuple) else e.source}")
    except Exception as e:
        errors_found = True
        print(f"✗ {template_name:40} - ERROR: {str(e)}")

print("=" * 60)
if errors_found:
    print("RESULT: ERRORS FOUND")
    sys.exit(1)
else:
    print("RESULT: ALL TEMPLATES VALID ✓")
    sys.exit(0)
