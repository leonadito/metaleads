"""Client selector for people with more than one client (agency staff: their
own tenant plus their clients; Agitare staff: every client, for support).

In metaleads each client is its own local account, so switching signs into
that client's account. Django flushes the session when the user changes, so
the single sign-on data (sub, tokens, list of clients) is carried over and the
new session is registered for the single logout.
"""
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from .models import OIDCSession, UserProfile
from .oidc import account_for_client

SELF = "self"  # Agitare staff: back to their own account
CARRY_OVER = ("tenant_choices", "agitare_staff")


def choices(request):
    """[{"id", "name"}] the person may switch to, current account excluded."""
    if not request.user.is_authenticated:
        return []
    if request.session.get("agitare_staff"):
        out = [
            {"id": str(p.core_client_uuid), "name": p.user.username}
            for p in UserProfile.objects.filter(core_client_uuid__isnull=False).select_related("user").order_by("user__username")
        ]
        out.insert(0, {"id": SELF, "name": "Minha conta (equipe Agitare)"})
        return out
    return [{"id": c["id"], "name": c.get("name") or c.get("slug")} for c in request.session.get("tenant_choices") or []]


def _target(request, client_id):
    if request.session.get("agitare_staff"):
        if client_id == SELF:
            return User.objects.filter(profile__core_user_uuid=request.session.get("oidc_sub")).first()
        profile = UserProfile.objects.filter(core_client_uuid=client_id).select_related("user").first()
        return profile.user if profile else None
    entry = next((c for c in request.session.get("tenant_choices") or [] if c["id"] == client_id), None)
    return account_for_client(entry) if entry else None


@login_required
@require_POST
def switch_client(request):
    target = _target(request, request.POST.get("client", ""))
    if target is None:
        return HttpResponseForbidden("Você não tem acesso a este cliente.")
    carried = {k: v for k, v in request.session.items() if k.startswith("oidc_") or k in CARRY_OVER}
    old_key = request.session.session_key
    login(request, target, backend="leads.oidc.CoreOIDCBackend")
    request.session.update(carried)
    request.session.save()
    OIDCSession.objects.filter(session_key=old_key).delete()
    if carried.get("oidc_sub"):
        OIDCSession.objects.update_or_create(
            session_key=request.session.session_key, defaults={"sub": carried["oidc_sub"]}
        )
    return redirect("dashboard")


def client_switcher(request):
    """Context processor: the selector shows only with two or more choices."""
    options = choices(request) if hasattr(request, "user") else []
    current = ""
    if options and request.user.is_authenticated:
        profile = UserProfile.objects.filter(user=request.user).first()
        current = SELF if (profile and profile.core_user_uuid) else str(getattr(profile, "core_client_uuid", "") or "")
    return {"client_choices": options if len(options) > 1 else [], "current_client": current}
