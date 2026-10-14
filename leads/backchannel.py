"""Single logout: receives the core's OIDC Back-Channel Logout.

When a person signs out of the Agitare Core, the core POSTs a signed
`logout_token` here and every session that person opened in this app through
single sign-on ends. Sessions are tracked in OIDCSession at login time.
"""
import logging
import time

from django.conf import settings
from django.contrib.auth.signals import user_logged_in
from django.contrib.sessions.models import Session
from django.dispatch import receiver
from django.http import HttpResponse, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import OIDCSession

logger = logging.getLogger(__name__)

EVENT = "http://schemas.openid.net/event/backchannel-logout"
MAX_AGE_SECONDS = 600


@receiver(user_logged_in)
def remember_sso_session(sender, request, user, **kwargs):
    sub = request.session.get("oidc_sub") if request is not None else None
    if sub and request.session.session_key:
        OIDCSession.objects.update_or_create(
            session_key=request.session.session_key, defaults={"sub": sub}
        )


def validate_logout_token(token):
    """Signature through the core's JWKS (mozilla-django-oidc), then the
    claims the Back-Channel Logout spec requires. Raises ValueError."""
    from .oidc import CoreOIDCBackend

    try:
        claims = CoreOIDCBackend().verify_token(token)
    except Exception as exc:  # bad signature, unknown key, malformed token
        raise ValueError(f"assinatura inválida: {exc}") from exc
    aud = claims.get("aud")
    if claims.get("iss") != settings.OIDC_OP_ISSUER:
        raise ValueError("emissor errado")
    if settings.OIDC_RP_CLIENT_ID not in (aud if isinstance(aud, list) else [aud]):
        raise ValueError("token para outro app")
    if EVENT not in (claims.get("events") or {}):
        raise ValueError("não é um logout_token")
    if "nonce" in claims:
        raise ValueError("logout_token não pode ter nonce")
    if not claims.get("sub"):
        raise ValueError("sem sub")
    if abs(time.time() - int(claims.get("iat", 0))) > MAX_AGE_SECONDS:
        raise ValueError("token velho")
    return claims


@csrf_exempt
@require_POST
def backchannel_logout(request):
    try:
        claims = validate_logout_token(request.POST.get("logout_token", ""))
    except ValueError as exc:
        logger.warning("Logout por canal de fundo recusado: %s", exc)
        return HttpResponseBadRequest(str(exc))
    tracked = OIDCSession.objects.filter(sub=claims["sub"])
    Session.objects.filter(session_key__in=tracked.values("session_key")).delete()
    ended = tracked.delete()[0]
    logger.info("Logout único: %s sessão(ões) encerrada(s) para %s", ended, claims["sub"])
    return HttpResponse(status=200)
