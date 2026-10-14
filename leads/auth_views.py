from django.conf import settings
from django.contrib.auth import views as auth_views
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import urlencode


class LoginView(auth_views.LoginView):
    """With single sign-on on, the login lives in the Agitare Core: send the
    visitor there (keeping ?next=). ?local=1 still shows the password form,
    for emergencies; after a logout or a refused login the page explains
    what happened instead of bouncing straight back to the core."""

    template_name = 'login.html'

    def get(self, request, *args, **kwargs):
        stay = any(k in request.GET for k in ('local', 'saiu', 'falhou'))
        if settings.OIDC_ENABLED and not stay and not request.user.is_authenticated:
            url = reverse('oidc_authentication_init')
            next_url = request.GET.get('next')
            return redirect(f"{url}?{urlencode({'next': next_url})}" if next_url else url)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['oidc_enabled'] = settings.OIDC_ENABLED
        return context
