"""Single sign-on with the Agitare Core (OIDC provider).

Enabled only when OIDC_RP_CLIENT_ID is set (see leadmanager/settings.py), so
production keeps its local login until the cutover.

In metaleads a local login is a *client* (one sheet per account), not a
person. So:
- a person with a client membership signs into that client's local account,
  linked by UserProfile.core_client_uuid; on the first SSO login an existing
  account whose username equals the client slug (case-insensitive, e.g.
  "Parigi" -> "parigi") is linked; otherwise a new account is created;
- Agitare staff (agitare_staff claim) get their own superuser account, one per
  person, linked by UserProfile.core_user_uuid (or by e-mail the first time).
Agency (whitelabel) memberships need the tenant selector, not built yet: such
users are refused for now, like users without any client membership.
"""
import logging
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from mozilla_django_oidc.auth import OIDCAuthenticationBackend

from .models import UserProfile

logger = logging.getLogger(__name__)


def _client_membership(claims):
    for m in claims.get("memberships") or []:
        if m.get("scope") == "client" and m.get("client_id"):
            return m
    return None


def _profile(user):
    profile, _ = UserProfile.objects.get_or_create(user=user)
    return profile


class CoreOIDCBackend(OIDCAuthenticationBackend):
    def verify_claims(self, claims):
        if not super().verify_claims(claims) or not claims.get("sub"):
            return False
        if claims.get("agitare_staff") or _client_membership(claims):
            return True
        logger.warning("Login único recusado: %s sem vínculo de cliente no core", claims.get("email"))
        return False

    def filter_users_by_claims(self, claims):
        if claims.get("agitare_staff"):
            users = User.objects.filter(profile__core_user_uuid=claims["sub"])
            if users.exists():
                return users
            email = (claims.get("email") or "").strip()
            return User.objects.filter(
                email__iexact=email, is_superuser=True, profile__core_user_uuid__isnull=True
            ) if email else User.objects.none()

        membership = _client_membership(claims)
        users = User.objects.filter(profile__core_client_uuid=membership["client_id"])
        if users.exists():
            return users
        slug = membership.get("client_slug") or ""
        if not slug:
            return User.objects.none()
        # Unlinked account named after the client. The profile may not exist yet.
        return User.objects.filter(username__iexact=slug).exclude(profile__core_client_uuid__isnull=False)

    @transaction.atomic
    def create_user(self, claims):
        if claims.get("agitare_staff"):
            base = (claims["email"].split("@")[0] or "equipe")[:140]
        else:
            base = _client_membership(claims).get("client_slug") or "cliente"
        username, n = base, 1
        while User.objects.filter(username__iexact=username).exists():
            n += 1
            username = f"{base}{n}"
        user = User(username=username)
        user.set_unusable_password()  # signs in only through the core
        user.save()
        return self.update_user(user, claims)

    @transaction.atomic
    def update_user(self, user, claims):
        if getattr(self, 'request', None) is not None:
            # Read by leads.backchannel at login to track this session.
            self.request.session['oidc_sub'] = claims['sub']
        profile = _profile(user)
        if claims.get("agitare_staff"):
            user.is_staff = user.is_superuser = True
            user.email = claims.get("email") or user.email
            user.first_name = claims.get("given_name") or user.first_name
            user.last_name = claims.get("family_name") or user.last_name
            profile.core_user_uuid = claims["sub"]
        else:
            # A client account is shared by the client's people: keep its
            # own name/e-mail, only link it to the core client.
            profile.core_client_uuid = _client_membership(claims)["client_id"]
        user.save()
        profile.save()
        return user


def provider_logout(request):
    """After the local logout, end the core's single sign-on session too."""
    params = {
        "post_logout_redirect_uri": settings.OIDC_POST_LOGOUT_REDIRECT_URI,
        "client_id": settings.OIDC_RP_CLIENT_ID,
    }
    id_token = request.session.get("oidc_id_token")
    if id_token:
        params["id_token_hint"] = id_token
    return f"{settings.OIDC_OP_LOGOUT_ENDPOINT}?{urlencode(params)}"
