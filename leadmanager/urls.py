from django.conf import settings
from django.contrib import admin
from django.urls import path, include
from django.contrib.auth import views as auth_views
from django.views.generic import RedirectView

from leads.auth_views import LoginView

if settings.OIDC_ENABLED:
    from mozilla_django_oidc.views import OIDCLogoutView

    # Ends the local session and then the core's (OIDC_OP_LOGOUT_URL_METHOD).
    logout_view = OIDCLogoutView.as_view()
else:
    logout_view = auth_views.LogoutView.as_view()

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', RedirectView.as_view(url='/dashboard/', permanent=False)),
    path('login/', LoginView.as_view(), name='login'),
    path('logout/', logout_view, name='logout'),
    path('', include('leads.urls')),
    path('api/', include('leads.api_urls')),
]

if settings.OIDC_ENABLED:
    # /oidc/authenticate/ (start) and /oidc/callback/ (return from the core).
    urlpatterns.insert(0, path('oidc/', include('mozilla_django_oidc.urls')))
    # Single logout: the core POSTs a logout_token here (leads/backchannel.py).
    from leads.backchannel import backchannel_logout

    urlpatterns.insert(0, path('oidc/backchannel-logout/', backchannel_logout, name='oidc_backchannel_logout'))
