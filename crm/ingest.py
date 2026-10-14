"""One entry point for every lead source (API, webhooks, importers).

Rules (design approved on 2026-10-14):
- the contact is found inside the tenant by phone (E.164), then e-mail, then a
  channel id (WhatsApp, Instagram, Messenger); blanks are filled, nothing that
  is already known is overwritten;
- the same source + external id never creates a second lead (a retried
  webhook or a re-sent form returns the lead that already exists);
- one open lead per contact: a new entry while the lead is open is recorded
  as a "reentry" event plus a new attribution row, not as a new lead;
- every entry gets a LeadAttribution row (platform data + original payload).
"""
from dataclasses import dataclass, field

import phonenumbers
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import (
    AttributionKind,
    Contact,
    EventType,
    Lead,
    LeadAttribution,
    LeadEvent,
    Platform,
    Source,
)

ATTRIBUTION_FIELDS = [
    "campaign_id", "campaign_name", "adset_id", "adset_name", "ad_id", "ad_name", "form_id", "form_name",
    "placement", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid",
    "ctwa_clid", "creative_headline", "creative_body", "creative_source_url", "creative_media_url",
]
CHANNEL_FIELDS = ["whatsapp_wa_id", "instagram_id", "messenger_psid"]


class IngestError(ValueError):
    pass


def normalize_phone(raw, region="BR"):
    """E.164 or "" when it is not a valid number."""
    if not raw:
        return ""
    try:
        number = phonenumbers.parse(str(raw), region)
    except phonenumbers.NumberParseException:
        return ""
    if not phonenumbers.is_valid_number(number):
        return ""
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


@dataclass
class Result:
    lead: Lead
    contact: Contact
    created: bool  # a new lead was opened
    duplicate: bool = False  # same source + external id seen before
    warnings: list = field(default_factory=list)


def _find_contact(tenant, phone, email, channels):
    qs = Contact.objects.select_for_update().filter(tenant=tenant)
    if phone:
        c = qs.filter(phone_e164=phone).first()
        if c:
            return c
    if email:
        c = qs.filter(email=email).first()
        if c:
            return c
    for name, value in channels.items():
        if value:
            c = qs.filter(**{name: value}).first()
            if c:
                return c
    return None


def _fill(contact, values):
    changed = False
    for name, value in values.items():
        if value not in (None, "") and getattr(contact, name) in (None, ""):
            setattr(contact, name, value)
            changed = True
    return changed


def ingest(tenant, data, actor="sistema"):
    """Records one lead entry. `data` is the API body (see crm/api.py)."""
    if not tenant.is_active:
        raise IngestError("tenant inativo")
    source = data.get("source") or Source.API
    if source not in Source.values:
        raise IngestError(f"source inválido: {source}")
    warnings = []
    phone = normalize_phone(data.get("phone"))
    if data.get("phone") and not phone:
        warnings.append("telefone inválido, guardado só nas respostas")
    email = (data.get("email") or "").strip().lower()
    channels = {name: str(data.get(name) or "").strip() for name in CHANNEL_FIELDS}
    if not (phone or email or any(channels.values())):
        raise IngestError("é preciso ao menos telefone, e-mail ou id de canal")
    external_id = str(data.get("external_id") or "").strip()[:128]

    for attempt in (1, 2):  # second try after a concurrent insert of the same contact
        try:
            with transaction.atomic():
                return _ingest(tenant, data, actor, source, phone, email, channels, external_id, warnings)
        except IntegrityError:
            if attempt == 2:
                raise


def _occurred_at(value, now):
    """When the entry really happened (e.g. the lead's created_time at Meta,
    for leads read later); never in the future."""
    if isinstance(value, str):
        value = parse_datetime(value)
    if value is None or timezone.is_naive(value):
        return now
    return min(value, now)


def _ingest(tenant, data, actor, source, phone, email, channels, external_id, warnings):
    now = timezone.now()
    occurred_at = _occurred_at(data.get("occurred_at"), now)
    if external_id:
        existing = Lead.objects.filter(tenant=tenant, source=source, external_id=external_id).first()
        if existing is None:
            # Re-entries keep their external id only in the history event.
            event = (
                LeadEvent.objects.select_related("lead")
                .filter(tenant=tenant, data__source=source, data__external_id=external_id)
                .first()
            )
            existing = event.lead if event else None
        if existing:
            return Result(existing, existing.contact, created=False, duplicate=True)

    contact = _find_contact(tenant, phone, email, channels)
    values = {
        "name": (data.get("name") or "").strip()[:255],
        "phone_e164": phone,
        "email": email,
        "city": (data.get("city") or "").strip()[:120],
        "company_name": (data.get("company") or "").strip()[:200],
        "country_code": (data.get("country_code") or "").strip().upper()[:2],
        **channels,
    }
    if contact is None:
        contact = Contact.objects.create(tenant=tenant, **values)
    elif _fill(contact, values):
        contact.save()

    consent = data.get("consent") or {}
    if consent:
        if "whatsapp" in consent:
            contact.consent_whatsapp = bool(consent["whatsapp"])
        if "email" in consent:
            contact.consent_email = bool(consent["email"])
        contact.consent_at = now
        contact.consent_source = str(consent.get("source") or source)[:200]
        contact.save()

    page = data.get("landing_page") or {}
    answers = data.get("form_answers") or {}
    lead = Lead.objects.select_for_update().filter(contact=contact).exclude(stage__in=["convertido", "perdido"]).first()
    created = lead is None
    if created:
        lead = Lead.objects.create(
            tenant=tenant,
            contact=contact,
            source=source,
            external_id=external_id,
            form_answers=answers,
            landing_page_uuid=page.get("id") or None,
            landing_page_name=(page.get("name") or "")[:255],
            landing_page_url=(page.get("url") or "")[:500],
            conversation_id=data.get("conversation_id") or None,
        )
        if occurred_at < now:  # read late (polling, backfill): keep the real date
            Lead.objects.filter(pk=lead.pk).update(created_at=occurred_at)
            lead.created_at = occurred_at
    else:
        merged = {**lead.form_answers, **answers}
        if merged != lead.form_answers:
            lead.form_answers = merged
        if data.get("conversation_id") and not lead.conversation_id:
            lead.conversation_id = data["conversation_id"]
        lead.save()

    attribution = data.get("attribution") or {}
    LeadAttribution.objects.create(
        lead=lead,
        tenant=tenant,
        occurred_at=occurred_at,
        platform=attribution.get("platform") if attribution.get("platform") in Platform.values else Platform.DIRECT,
        kind=attribution.get("kind") if attribution.get("kind") in AttributionKind.values else AttributionKind.OTHER,
        is_organic=attribution.get("is_organic"),
        raw=attribution.get("raw") or {},
        **{f: str(attribution.get(f) or "")[: LeadAttribution._meta.get_field(f).max_length] for f in ATTRIBUTION_FIELDS},
    )
    LeadEvent.objects.create(
        lead=lead,
        tenant=tenant,
        type=EventType.CREATED if created else EventType.REENTRY,
        actor=actor,
        data={"source": source, "external_id": external_id, "form_answers": answers, "warnings": warnings},
    )
    return Result(lead, contact, created=created, warnings=warnings)
