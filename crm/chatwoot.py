"""Conversations arriving through Chatwoot become leads (Phase 4).

Each tenant's Chatwoot account has a webhook (created by the core) that POSTs
`message_created` here, signed by Chatwoot with the webhook's own secret:
    X-Chatwoot-Timestamp: <unix>
    X-Chatwoot-Signature: sha256=<hex HMAC-SHA256(secret, "<timestamp>.<body>")>
The secret is read from the core (GET /api/v1/tenants/<uuid>/chatwoot) and
cached in the tenant copy; on a mismatch it is read again once (rotation).

The receiver only checks and queues; the task does the work:
- the first message *from the contact* in a conversation opens (or re-enters)
  a lead through crm.ingest, source = channel of the inbox, external id =
  the conversation, contact = Chatwoot's contact (phone, e-mail, channel id);
- a Click-to-WhatsApp ad shows up as `content_attributes.referral` on that
  first message: it becomes a LeadAttribution (Meta, ctwa) with the ad id,
  ctwa_clid, creative and the original referral;
- every public message (incoming or outgoing) of a conversation that has a
  lead becomes a "message" event with the message id only, never the text
  (conversations live only in Chatwoot, decision of 2026-10-12).
"""
import hashlib
import hmac
import json
import logging
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import requests
from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django_tasks import task

from . import chatwoot_actions, messaging, routing
from .ingest import IngestError, ingest
from .models import EventType, Lead, LeadEvent, Source, Tenant

logger = logging.getLogger(__name__)

TOLERANCE_SECONDS = 300
INCOMING, OUTGOING, ACTIVITY, TEMPLATE = 0, 1, 2, 3
MESSAGE_TYPES = {INCOMING: "incoming", OUTGOING: "outgoing", ACTIVITY: "activity", TEMPLATE: "template"}

CHANNEL_SOURCE = {
    "Channel::Whatsapp": Source.WHATSAPP,
    "Channel::Instagram": Source.INSTAGRAM,
    "Channel::FacebookPage": Source.MESSENGER,
    "Channel::Email": Source.EMAIL,
    "Channel::WebWidget": Source.SITE_CHAT,
    "Channel::Api": Source.API,
}
CHANNEL_ID_FIELD = {
    Source.WHATSAPP: "whatsapp_wa_id",
    Source.INSTAGRAM: "instagram_id",
    Source.MESSENGER: "messenger_psid",
}
_TYPE_BY_NAME = {"incoming": INCOMING, "outgoing": OUTGOING, "activity": ACTIVITY, "template": TEMPLATE}


def _message_type(value):
    return _TYPE_BY_NAME.get(value, value) if isinstance(value, str) else value


def fetch_secret(tenant):
    """Reads the account credentials again from the core (the bot token and
    administrator come along) and returns the webhook secret."""
    return chatwoot_actions.fetch_credentials(tenant).get("webhook_secret") or ""


def _valid(secret, body, timestamp, signature, now=None):
    if not (secret and timestamp and signature.startswith("sha256=")):
        return False
    try:
        if abs((now or time.time()) - int(timestamp)) > TOLERANCE_SECONDS:
            return False
    except ValueError:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature[7:], expected)


@csrf_exempt
@require_POST
def chatwoot_webhook(request):
    try:
        payload = json.loads(request.body)
        account_id = int(payload["account"]["id"])
    except (ValueError, KeyError, TypeError):
        return HttpResponseBadRequest("evento malformado")
    tenant = Tenant.objects.filter(chatwoot_account_id=account_id).first()
    if tenant is None:
        return HttpResponseForbidden("conta desconhecida")
    timestamp = request.headers.get("X-Chatwoot-Timestamp", "")
    signature = request.headers.get("X-Chatwoot-Signature", "")
    if not _valid(tenant.chatwoot_webhook_secret, request.body, timestamp, signature):
        # First call, or the secret changed: read it again from the core, once.
        if not _valid(fetch_secret(tenant), request.body, timestamp, signature):
            return HttpResponseForbidden("assinatura inválida")
    event = payload.get("event")
    if event == "message_created":
        process_message.enqueue(tenant.pk, payload)
    elif event in ("conversation_updated", "conversation_status_changed"):
        chatwoot_actions.conversation_changed.enqueue(tenant.pk, payload)
    return HttpResponse(status=200)


def _attribution(message, conversation, source):
    referral = (message.get("content_attributes") or {}).get("referral") or {}
    if referral:
        is_ad = referral.get("source_type") == "ad"
        return {
            "platform": "meta",
            "kind": "ctwa",
            "placement": "whatsapp" if source == Source.WHATSAPP else source,
            "ad_id": referral.get("source_id", "") if is_ad else "",
            "ctwa_clid": referral.get("ctwa_clid", ""),
            "creative_headline": referral.get("headline", ""),
            "creative_body": referral.get("body", ""),
            "creative_source_url": referral.get("source_url", ""),
            "creative_media_url": referral.get("image_url") or referral.get("video_url") or "",
            "is_organic": not is_ad,
            "raw": {"referral": referral},
        }
    extra = conversation.get("additional_attributes") or {}
    referer = extra.get("referer") or ""
    if referer:  # site chat: the page the visitor was on, with its UTMs
        query = {k: v[0] for k, v in parse_qs(urlparse(referer).query).items()}
        utm = {k: query.get(k, "") for k in ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid")}
        platform = "meta" if utm["fbclid"] else "google" if utm["gclid"] else "other" if utm["utm_source"] else "direct"
        return {"platform": platform, "kind": "landing_page", **utm, "raw": {"conversation": extra}}
    return {"platform": "direct", "kind": "other", "raw": {"conversation": extra} if extra else {}}


def _lead_for_conversation(tenant, conversation_id):
    return Lead.objects.filter(tenant=tenant, conversation_id=conversation_id).order_by("-created_at").first()


LEADLESS_RETRIES = 3
LEADLESS_WAIT = timedelta(seconds=30)


@task
def process_message(tenant_id, payload, attempt=0):
    tenant = Tenant.objects.filter(pk=tenant_id).first()
    if tenant is None or payload.get("private"):
        return "ignorado"
    mtype = _message_type(payload.get("message_type"))
    if mtype not in (INCOMING, OUTGOING, TEMPLATE):
        return "ignorado"  # activity messages (status changes etc.)
    conversation = payload.get("conversation") or {}
    conversation_id = conversation.get("id")
    if not conversation_id:
        return "sem conversa"
    sender = payload.get("sender") or {}
    if mtype == INCOMING and messaging.is_bounce(sender.get("email")):
        # Delivery failure notice in the e-mail inbox: never a lead; the
        # addresses the CRM e-mailed go to the suppression list.
        found = messaging.record_bounce(tenant, payload.get("content") or "")
        return f"devolução: {', '.join(found) or 'sem endereço nosso'}"

    lead = _lead_for_conversation(tenant, conversation_id)
    if lead is None and mtype == INCOMING:
        source = CHANNEL_SOURCE.get(conversation.get("channel"), Source.API)
        sender = payload.get("sender") or {}
        data = {
            "source": source,
            "external_id": f"chatwoot:{tenant.chatwoot_account_id}:{conversation_id}",
            "name": sender.get("name") or "",
            "phone": sender.get("phone_number") or "",
            "email": sender.get("email") or "",
            "conversation_id": conversation_id,
            "attribution": _attribution(payload, conversation, source),
        }
        channel_field = CHANNEL_ID_FIELD.get(source)
        source_id = (conversation.get("contact_inbox") or {}).get("source_id") or ""
        if channel_field and source_id:
            data[channel_field] = source_id
        try:
            result = ingest(tenant, data, actor="chatwoot")
        except IngestError as exc:
            logger.warning("Conversa %s do Chatwoot não virou lead: %s", conversation_id, exc)
            return f"recusado: {exc}"
        lead = result.lead
        if sender.get("id") and not result.contact.chatwoot_contact_id:
            result.contact.chatwoot_contact_id = sender["id"]
            result.contact.save(update_fields=["chatwoot_contact_id", "updated_at"])
    if lead is None:
        # Chatwoot may deliver an agent's reply before the contact's message
        # that opens the lead (seen in the beta: 9 ms apart). Try again a bit
        # later; if the conversation really has no lead (agent wrote first),
        # give up after a few tries.
        if attempt < LEADLESS_RETRIES:
            process_message.using(run_after=timezone.now() + LEADLESS_WAIT).enqueue(tenant_id, payload, attempt + 1)
            return "conversa ainda sem lead; nova tentativa agendada"
        return "conversa sem lead"

    message_id = payload.get("id")
    if not LeadEvent.objects.filter(lead=lead, type=EventType.MESSAGE, data__message_id=message_id).exists():
        sender = payload.get("sender") or {}
        chatwoot_actions.message_seen(
            lead, MESSAGE_TYPES[mtype], sender.get("type") or ("contact" if mtype == INCOMING else ""),
        )
        if mtype == INCOMING:
            routing.schedule(lead)  # the AI decides where the lead belongs (Phase 7)
        LeadEvent.objects.create(
            lead=lead,
            tenant=tenant,
            type=EventType.MESSAGE,
            actor="contato" if mtype == INCOMING else (sender.get("name") or "atendimento"),
            data={
                "message_id": message_id,
                "conversation_id": conversation_id,
                "direction": MESSAGE_TYPES[mtype],
                "sender_type": sender.get("type") or ("contact" if mtype == INCOMING else ""),
                "chatwoot_sender_id": sender.get("id"),
            },
        )
    return str(lead.uuid)
