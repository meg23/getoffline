#!/usr/bin/env python
import os
import sys
import django

os.environ['GETOFFLINE_DJANGO_ROLE'] = 'api'
os.environ['DJANGO_SETTINGS_MODULE'] = 'frontend.settings'
django.setup()

from django.test import RequestFactory
from django.contrib.auth.models import User
from api.views import frontend_settings

factory = RequestFactory()
user = User.objects.get(username='testuser')
request = factory.get('/settings/')
request.user = user

response = frontend_settings(request)
content = response.content.decode('utf-8')

# Check for censor fields
if 'censor_profanity' in content:
    print("✓ Found censor_profanity field in HTML")
else:
    print("✗ censor_profanity field NOT found in HTML")

if 'censor_method' in content:
    print("✓ Found censor_method field in HTML")
else:
    print("✗ censor_method field NOT found in HTML")

# Check for YouTube and Podcast sections
if 'youtube' in content.lower() and 'source' in content:
    print("✓ Found YouTube section")
else:
    print("✗ YouTube section NOT found")

if 'podcast' in content.lower():
    print("✓ Found Podcast section")
else:
    print("✗ Podcast section NOT found")

# Print first few lines to verify content
print("\nFirst 1000 characters of response:")
print(content[:1000])
