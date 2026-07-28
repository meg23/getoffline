#!/usr/bin/env python
import os
import sys
import django

os.environ['GETOFFLINE_DJANGO_ROLE'] = 'api'
os.environ['DJANGO_SETTINGS_MODULE'] = 'frontend.settings'
django.setup()

from django.test import RequestFactory
from django.contrib.auth.models import User
from api.views import frontend_settings, frontend_jobs
from frontend.views import library, player

factory = RequestFactory()

try:
    user = User.objects.get(username='testuser')
except User.DoesNotExist:
    print("Error: testuser not found. Please create it first.")
    sys.exit(1)

print("=" * 70)
print("DJANGO VIEW RENDERING TEST")
print("=" * 70)
print()

test_views = [
    ('Settings', lambda: frontend_settings(RequestFactory().get('/settings/'))),
    ('Jobs', lambda: frontend_jobs(RequestFactory().get('/jobs/'))),
    ('Library', lambda: library(RequestFactory().get('/'))),
    ('Player', lambda: player(RequestFactory().get('/player/?id=test'))),
]

errors = []
for view_name, view_func in test_views:
    try:
        request = RequestFactory().get('/')
        request.user = user
        response = view_func()
        if response.status_code == 200:
            print(f"✓ {view_name:30} - HTTP 200 OK")
        else:
            print(f"✗ {view_name:30} - HTTP {response.status_code}")
            errors.append(f"{view_name}: HTTP {response.status_code}")
    except Exception as e:
        print(f"✗ {view_name:30} - ERROR: {str(e)[:50]}")
        errors.append(f"{view_name}: {str(e)}")

print()
print("=" * 70)
if errors:
    print(f"RESULT: {len(errors)} view(s) failed")
    for error in errors:
        print(f"  • {error}")
    sys.exit(1)
else:
    print("RESULT: All views render successfully ✓")
    sys.exit(0)
