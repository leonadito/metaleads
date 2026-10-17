"""The CRM tab next to each Chatwoot conversation (Dashboard App, Phase 7).

Agents work only in Chatwoot and have no login here, so the tab is read-only
and authenticated by a code in its URL: an HMAC of the client's uuid with a
secret shared with the core, which registers the tab in each account. The
code is visible to the account's agents, who already see these
conversations. Chatwoot tells the iframe which conversation is open through
postMessage, without a signature, so the page only accepts messages from
the Chatwoot origin and only shows a lead of the code's own tenant.
"""
import hashlib
import hmac

from django.conf import settings
from django.http import Http404
from django.shortcuts import render
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.http import require_safe

from . import summary
from .models import Lead, ReplyDraft, Tenant


def code_for(tenant_uuid):
    return hmac.new(settings.CHATWOOT_CRM_APP_SECRET.encode(), str(tenant_uuid).encode(), hashlib.sha256).hexdigest()[:40]


def _tenant(tenant_uuid, code):
    if not settings.CHATWOOT_CRM_APP_SECRET or not hmac.compare_digest(code_for(tenant_uuid), code):
        raise Http404
    tenant = Tenant.objects.filter(uuid=tenant_uuid).first()
    if tenant is None:
        raise Http404
    return tenant


def _framed(response):
    # Only Chatwoot may show this page in a frame.
    response["Content-Security-Policy"] = f"frame-ancestors {settings.CHATWOOT_URL or 'none'}"
    return response


@xframe_options_exempt
@require_safe
def app(request, tenant_uuid, code):
    _tenant(tenant_uuid, code)
    return _framed(render(request, "crm/dashboard_app.html", {
        "card_url": f"/chatwoot/app/{tenant_uuid}/{code}/conversa/", "chatwoot_origin": settings.CHATWOOT_URL,
    }))


@xframe_options_exempt
@require_safe
def card(request, tenant_uuid, code, conversation_id):
    from .views import _history

    tenant = _tenant(tenant_uuid, code)
    lead = (Lead.objects.select_related("contact", "substage").filter(tenant=tenant, conversation_id=conversation_id)
            .order_by("-created_at").first())
    context = {"lead": lead}
    if lead:
        context.update(groups=summary.groups(lead), history=_history(lead)[:8],
                       draft=ReplyDraft.objects.filter(lead=lead, status=ReplyDraft.Status.DRAFT).exists())
    return _framed(render(request, "crm/_dashboard_card.html", context))
