import os
import sys
sys.path.insert(0, '/app/src')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'frontend.settings')
import django
django.setup()

from django.test import RequestFactory
from api.views import frontend_settings
from django.contrib.auth.models import User

# Create a request
factory = RequestFactory()
request = factory.get('/api/frontend/settings/')

# Set the user
try:
    request.user = User.objects.get(username='testuser')
except User.DoesNotExist:
    request.user = User.objects.create_user('testuser', password='test123')

# Call the view
try:
    response = frontend_settings(request)
    print(f'Status: {response.status_code}')
    if response.status_code != 200:
        print(f'Error Content: {response.content[:2000].decode()}')
except Exception as e:
    import traceback
    traceback.print_exc()
