"""
URL configuration for ttstats project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
import os

from django.contrib import admin
from django.urls import path, include
from django.views.generic import RedirectView

from pingpong.views import CustomLoginView

# Admin URL can be customized via environment variable for security
# Set ADMIN_URL to a random string in production (e.g., 'secret-admin-abc123/')
ADMIN_URL = os.environ.get('ADMIN_URL', 'admin/')

urlpatterns = [
    path('', RedirectView.as_view(url='pingpong/', permanent=False)),
    path(ADMIN_URL, admin.site.urls),
    path('accounts/login/', CustomLoginView.as_view(), name='login'),
    path('accounts/', include('django.contrib.auth.urls')),
    path('pingpong/', include('pingpong.urls')),
    # WebAuthn URLs (must be at root level for namespace to work)
    path('webauthn/', include(('django_otp_webauthn.urls', 'otp_webauthn'))),
]
