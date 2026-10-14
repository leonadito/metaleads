"""Single sign-on with the Agitare Core (OIDC provider).

Enabled only when OIDC_RP_CLIENT_ID is set (see leadmanager/settings.py), so
production keeps its local login until the cutover.

In metaleads a local login is a *client* (one sheet per account), not a
person. So:
- a person signs into the local account of a client they may use, linked by
  UserProfile.core_client_uuid; on the first SSO login an existing account
  whose username equals the client slug (case-insensitive, e.g. "Parigi" ->
  "parigi") is linked; otherwise a new account is created;
- the clients a person may use come from the memberships: a client
  membership gives that client; an agency membership gives the agency's own
  tenant (listed first, where the login lands) and its active clients. The
  list goes to the session ("tenant_choices") for the selector
  (leads/tenant_switch.py), which signs into another client's account;
- Agitare staff (agitare_staff claim) get their own superuser account, one per
  person, linked by UserProfile.core_user_uuid (or by e-mail the first time),
  and may switch into any client account for support.
"""
import logging
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from mozilla_django_oidc.auth import OIDCAuthenticationBackend

from .models import UserProfile

logger = logging.getLogger(__name__)


def accessible_clients(claims):
    """Every core client this person may use, in claim order, as
    {"id", "slug", "name"} dicts (agency's own tenant first)."""
    seen, out = set(), []
    for m in claims.get("memberships") or []:
        if m.get("scope") == "client" and m.get("client_id"):
            entries = [{"id": m["client_id"], "slug": m.get("client_slug"), "name": m.get("client_name")}]
        elif m.get("scope") == "whitelabel":
            entries = [{"id": c["id"], "slug": c.get("slug"), "name": c.get("name")} for c in m.get("clients") or []]
        else:
            entries = []
        for e in entries:
            if e["id"] not in seen:
                seen.add(e["id"])
                out.append(e)
    return out


def _profile(user):
    profile, _ = UserProfile.objects.get_or_create(user=user)
    return profile


def find_client_account(entry):
    """Queryset with the local account of a core client: the linked one, else
    an unlinked account named after the client's slug."""
    users = User.objects.filter(profile__core_client_uuid=entry["id"])
    if users.exists() or not entry.get("slug"):
        return users
    return User.objects.filter(username__iexact=entry["slug"]).exclude(profile__core_client_uuid__isnull=False)


@transaction.atomic
def account_for_client(entry):
    """The client's local account, linked to the core client; created when the
    client has none yet (e.g. a client that so far only used agipages)."""
    user = find_client_account(entry).first()
    if user is None:
        base = entry.get("slug") or "cliente"
        username, n = base, 1
        while User.objects.filter(username__iexact=username).exists():
            n += 1
            username = f"{base}{n}"
        user = User(username=username)
        user.set_unusable_password()  # signs in only through the core
        user.save()
    profile = _profile(user)
    if profile.core_client_uuid is None:
        profile.core_client_uuid = entry["id"]
        profile.save(update_fields=["core_client_uuid"])
    return user


class CoreOIDCBackend(OIDCAuthenticationBackend):
    def verify_claims(self, claims):
        if not super().verify_claims(claims) or not claims.get("sub"):
            return False
        if claims.get("agitare_staff") or accessible_clients(claims):
            return True
        logger.warning("Login único recusado: %s sem nenhum cliente no core", claims.get("email"))
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
        return find_client_account(accessible_clients(claims)[0])

    @transaction.atomic
    def create_user(self, claims):
        if claims.get("agitare_staff"):
            base = (claims["email"].split("@")[0] or "equipe")[:140]
            username, n = base, 1
            while User.objects.filter(username__iexact=username).exists():
                n += 1
                username = f"{base}{n}"
            user = User(username=username)
            user.set_unusable_password()
            user.save()
        else:
            user = account_for_client(accessible_clients(claims)[0])
        return self.update_user(user, claims)

    @transaction.atomic
    def update_user(self, user, claims):
        staff = bool(claims.get("agitare_staff"))
        clients = accessible_clients(claims)
        if getattr(self, 'request', None) is not None:
            # oidc_sub: read by leads.backchannel to track this session;
            # tenant_choices/agitare_staff: read by the selector.
            self.request.session['oidc_sub'] = claims['sub']
            self.request.session['tenant_choices'] = clients
            self.request.session['agitare_staff'] = staff
        profile = _profile(user)
        if staff:
            user.is_staff = user.is_superuser = True
            user.email = claims.get("email") or user.email
            user.first_name = claims.get("given_name") or user.first_name
            user.last_name = claims.get("family_name") or user.last_name
            profile.core_user_uuid = claims["sub"]
        elif profile.core_client_uuid is None:
            # A client account is shared by the client's people: keep its
            # own name/e-mail, only link it to the core client.
            profile.core_client_uuid = clients[0]["id"]
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
