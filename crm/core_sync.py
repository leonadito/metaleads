"""Local copy of the core's tenants (clients).

- Webhook receiver (POST /webhooks/core/): the core sends tenant.created,
  tenant.updated and tenant.deactivated, signed with HMAC-SHA256
  (X-Agitare-Signature: t=<unix>,v1=<hex of HMAC(secret, "t.body")>). Each
  event id is applied once (the core may deliver twice).
- Full sync (manage.py sync_tenants): pulls every tenant from the core's
  GET /api/v1/tenants, daily, to correct anything a lost webhook left behind.
Tenants are never deleted here: deactivation only changes the status.
"""
import hashlib
import hmac
import json
import logging
import time

import requests
from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import CoreWebhookReceipt, Tenant

logger = logging.getLogger(__name__)
TOLERANCE_SECONDS = 300


def verify_signature(secret, body, header, now=None):
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        timestamp, signature = int(parts["t"]), parts["v1"]
    except (KeyError, ValueError):
        return False
    now = int(time.time()) if now is None else now
    if not secret or abs(now - timestamp) > TOLERANCE_SECONDS:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def apply_tenant(payload):
    """Upsert one tenant from the core's payload (Client.as_payload)."""
    updated_at = parse_datetime(payload.get("updated_at") or "") if payload.get("updated_at") else None
    tenant = Tenant.objects.filter(uuid=payload["id"]).first()
    if tenant and tenant.core_updated_at and updated_at and updated_at < tenant.core_updated_at:
        return tenant  # an older event arriving late never overwrites newer data
    values = {
        "name": payload.get("name") or "",
        "slug": payload.get("slug") or "",
        "whitelabel_uuid": payload.get("whitelabel_id") or None,
        "status": payload.get("status") or "active",
        "is_courtesy": bool(payload.get("is_courtesy")),
        "chatwoot_account_id": payload.get("chatwoot_account_id"),
        "core_updated_at": updated_at,
    }
    tenant, _ = Tenant.objects.update_or_create(uuid=payload["id"], defaults=values)
    return tenant


@csrf_exempt
@require_POST
def core_webhook(request):
    if not verify_signature(settings.CORE_WEBHOOK_SECRET, request.body, request.headers.get("X-Agitare-Signature", "")):
        return HttpResponseForbidden("assinatura inválida")
    try:
        envelope = json.loads(request.body)
        event_id, event_type = envelope["id"], envelope["type"]
    except (ValueError, KeyError):
        return HttpResponseBadRequest("evento malformado")
    if not event_type.startswith("tenant."):
        return HttpResponse(status=204)  # not for us; acknowledged so the core does not retry
    try:
        with transaction.atomic():
            CoreWebhookReceipt.objects.create(event_id=event_id, event_type=event_type)
            apply_tenant(envelope["data"]["tenant"])
    except IntegrityError:
        pass  # already applied
    return HttpResponse(status=200)


def full_sync():
    """Returns how many tenants were upserted."""
    response = requests.get(
        f"{settings.CORE_API_URL.rstrip('/')}/api/v1/tenants",
        headers={"Authorization": f"Bearer {settings.CORE_WEBHOOK_SECRET}"},
        timeout=30,
    )
    response.raise_for_status()
    count = 0
    for payload in response.json()["tenants"]:
        apply_tenant(payload)
        count += 1
    return count
