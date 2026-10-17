"""Messages the CRM sends to leads (Phase 6), always through the tenant's
Chatwoot account, so people see everything in the same conversation.

What is sent:
- the initial message of a form lead (InitialMessage of the form, or the
  tenant's default): e-mail and/or a WhatsApp template, because the company
  is starting the conversation;
- the message of a sub-stage, when the lead enters it, in the lead's
  conversation.

Rules, enforced here in code (not in prompts):
- consent: the contact accepted being contacted on that channel, or the lead
  came from a platform form whose privacy policy covers it (Meta, Google);
- suppression list (unsubscribe, permanent bounce, complaint, manual);
- resend window: the same message (initial, or the same sub-stage) does not
  go again to the same contact within the tenant's window, unless a person
  resends it on purpose (the reason is recorded);
- 24-hour window: WhatsApp, Instagram and Messenger only take free text while
  Chatwoot says the conversation can be replied to (`can_reply`); outside it,
  WhatsApp needs an approved template and the others do not send;
- e-mail limits per mailbox: per hour and per day, with warm-up for a new
  mailbox, spaced sends, answers before first contacts. Nothing is dropped:
  what is over the limit waits in the queue.

The bot cannot create contacts; the core does it (POST .../chatwoot/contatos).
"""
import html
import logging
import re
import secrets
from datetime import datetime, time, timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django_tasks import task
from django_tasks_db.models import DBTaskResult

from .chatwoot_actions import ChatwootUnavailable, bot_api
from .models import (
    Channel, EventType, InitialMessage, Lead, LeadEvent, OutboundMessage, Source, Suppression, Tenant, TrackedLink,
)

logger = logging.getLogger(__name__)

FORM_SOURCES = {Source.AGIPAGES_FORM, Source.META_FORM, Source.GOOGLE_ADS_FORM}
CONSENT_BY_PLATFORM = {Source.META_FORM, Source.GOOGLE_ADS_FORM}  # the form's own privacy policy
SOURCE_CHANNEL = {
    Source.WHATSAPP: Channel.WHATSAPP, Source.INSTAGRAM: Channel.INSTAGRAM, Source.MESSENGER: Channel.MESSENGER,
    Source.SITE_CHAT: Channel.SITE_CHAT, Source.API: Channel.API, Source.EMAIL: Channel.EMAIL,
}
INBOX_TYPE = {Channel.EMAIL: "Channel::Email", Channel.WHATSAPP: "Channel::Whatsapp"}
WINDOW_CHANNELS = {Channel.WHATSAPP, Channel.INSTAGRAM, Channel.MESSENGER}
PRIORITY_ANSWER, PRIORITY_FIRST_CONTACT = 1, 5
MAX_ATTEMPTS = 5
WARMUP = [(7, 20), (14, 50), (21, 100)]  # (days since the mailbox's first e-mail, daily limit)
QUEUE_PATH = "crm.messaging.process_queue"
# Mailbox health (values to calibrate in the beta): over the last 7 days, at
# least HEALTH_MIN_SENT e-mails and HEALTH_MAX_BOUNCE_RATE of them bounced,
# or HEALTH_MAX_COMPLAINTS complaints, pause the tenant's automatic e-mail.
HEALTH_WINDOW = timedelta(days=7)
HEALTH_MIN_SENT = 20
HEALTH_MAX_BOUNCE_RATE = 0.05
HEALTH_MAX_COMPLAINTS = 3
INITIAL_MAX_AGE = timedelta(hours=24)
QUEUE_EVERY = timedelta(minutes=1)
VARIABLES = ("nome", "primeiro_nome", "pagina", "formulario", "empresa")

DEFAULT_SUBJECT = "Recebemos seu contato"
DEFAULT_EMAIL = (
    "Olá, {primeiro_nome}!\n\n"
    "Recebemos seu contato pelo formulário {pagina}. Em breve alguém da {empresa} fala com você.\n\n"
    "Se preferir, é só responder este e-mail."
)


# ── Variables ────────────────────────────────────────────────────────────────

def variables(lead):
    contact = lead.contact
    first = lead.attributions.order_by("occurred_at").first()
    form = (first.form_name if first else "") or lead.landing_page_name
    profile = lead.tenant.profile or {}
    name = contact.name or ""
    return {
        "nome": name or "tudo bem",
        "primeiro_nome": name.split()[0] if name.strip() else "tudo bem",
        "pagina": lead.landing_page_name or form or "do site",
        "formulario": form or "",
        "empresa": profile.get("trade_name") or lead.tenant.name,
    }


def render(text, values):
    """Only the known {variables}; never a template engine."""
    return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1), m.group(0))) if m.group(1) in VARIABLES else m.group(0),
                  text or "")


# ── Configuration ────────────────────────────────────────────────────────────

def default_initial_message(tenant):
    message, _ = InitialMessage.objects.get_or_create(
        tenant=tenant, source="", form_key="",
        defaults={"form_label": "Padrão", "email_subject": DEFAULT_SUBJECT, "email_body": DEFAULT_EMAIL,
                  "whatsapp_params": ["primeiro_nome", "pagina"]},
    )
    return message


def form_of(lead):
    """(source, form key, label) of a form lead: the agipages page, or the
    Meta / Google form."""
    if lead.source == Source.AGIPAGES_FORM:
        return lead.source, str(lead.landing_page_uuid or ""), lead.landing_page_name
    first = lead.attributions.order_by("occurred_at").first()
    return lead.source, (first.form_id if first else ""), (first.form_name if first else "") or (first.form_id if first else "")


def initial_message_for(lead):
    source, key, _ = form_of(lead)
    own = InitialMessage.objects.filter(tenant=lead.tenant, source=source, form_key=key).first() if key else None
    return own or default_initial_message(lead.tenant)


def inboxes(tenant, refresh=False):
    if tenant.chatwoot_inboxes and not refresh:
        return tenant.chatwoot_inboxes
    if not (settings.CORE_API_URL and tenant.chatwoot_account_id):
        return []
    response = requests.get(
        f"{settings.CORE_API_URL.rstrip('/')}/api/v1/tenants/{tenant.uuid}/chatwoot/inboxes",
        headers={"Authorization": f"Bearer {settings.CORE_WEBHOOK_SECRET}"}, timeout=20,
    )
    if response.status_code != 200:
        return tenant.chatwoot_inboxes or []
    found = response.json().get("inboxes", [])
    Tenant.objects.filter(pk=tenant.pk).update(chatwoot_inboxes=found)
    tenant.chatwoot_inboxes = found
    return found


def inbox_for(tenant, channel):
    kind = INBOX_TYPE.get(channel)
    return next((i for i in inboxes(tenant) if i.get("channel_type") == kind), None) if kind else None


# ── Gates ────────────────────────────────────────────────────────────────────

def consent_ok(lead, channel):
    contact = lead.contact
    given = contact.consent_email if channel == Channel.EMAIL else contact.consent_whatsapp
    if given is False:
        return False
    return bool(given) or lead.source in CONSENT_BY_PLATFORM


def address_of(lead, channel):
    return lead.contact.email if channel == Channel.EMAIL else lead.contact.phone_e164


def is_suppressed(tenant, address):
    return bool(address) and Suppression.objects.filter(tenant=tenant, address=address.lower()).exists()


def sent_recently(lead, dedupe_key, tenant):
    since = timezone.now() - timedelta(days=tenant.resend_window_days)
    return OutboundMessage.objects.filter(
        tenant=tenant, lead__contact=lead.contact, dedupe_key=dedupe_key,
        status__in=[OutboundMessage.SENT, OutboundMessage.QUEUED], created_at__gte=since,
    ).exists()


# ── Planning ─────────────────────────────────────────────────────────────────

def _record(lead, kind, channel, dedupe_key, status=OutboundMessage.QUEUED, reason="", **fields):
    msg = OutboundMessage.objects.create(
        tenant=lead.tenant, lead=lead, kind=kind, channel=channel, dedupe_key=dedupe_key, status=status,
        reason=reason, send_after=timezone.now(), **fields,
    )
    if status == OutboundMessage.SKIPPED:
        LeadEvent.objects.create(lead=lead, tenant=lead.tenant, type=EventType.MESSAGE_SKIPPED, actor="sistema",
                                 reason=reason, data={"outbound": str(msg.uuid), "kind": kind, "channel": channel})
    else:
        transaction.on_commit(schedule_queue)
    return msg


def _plan(lead, kind, channel, dedupe_key, forced_by="", **fields):
    """One message, or one skipped record saying why."""
    tenant = lead.tenant
    address = address_of(lead, channel)
    if channel in (Channel.EMAIL, Channel.WHATSAPP) and not fields.get("conversation_id"):
        if not address:
            return _record(lead, kind, channel, dedupe_key, OutboundMessage.SKIPPED, "contato sem esse canal", **fields)
        if not consent_ok(lead, channel):
            return _record(lead, kind, channel, dedupe_key, OutboundMessage.SKIPPED, "sem consentimento para esse canal", **fields)
        inbox = inbox_for(tenant, channel)
        if inbox is None:
            return _record(lead, kind, channel, dedupe_key, OutboundMessage.SKIPPED,
                           f"a conta do Chatwoot não tem inbox de {Channel(channel).label}", **fields)
        fields["inbox_id"] = inbox["id"]
    if is_suppressed(tenant, address):
        return _record(lead, kind, channel, dedupe_key, OutboundMessage.SKIPPED, "endereço na lista de bloqueio", **fields)
    if not forced_by and sent_recently(lead, dedupe_key, tenant):
        return _record(lead, kind, channel, dedupe_key, OutboundMessage.SKIPPED,
                       f"já enviada a este contato nos últimos {tenant.resend_window_days} dias", **fields)
    return _record(lead, kind, channel, dedupe_key, forced_by=forced_by, **fields)


def on_new_lead(lead):
    """Called by ingest when a lead is opened (inside its transaction)."""
    if lead.source in FORM_SOURCES and not lead.conversation_id:
        pk = lead.pk
        transaction.on_commit(lambda: plan_initial.enqueue(pk))


@task
def plan_initial(lead_id, forced_by=""):
    lead = Lead.objects.select_related("tenant", "contact").filter(pk=lead_id).first()
    if lead is None or not lead.tenant.chatwoot_account_id:
        return "sem conta no Chatwoot"
    config = initial_message_for(lead)
    if not config.is_active:
        return "mensagem inicial desligada"
    if not forced_by and timezone.now() - lead.created_at > INITIAL_MAX_AGE:
        # Read late (the Meta 90-day backfill, a batch of old leads): a first
        # message weeks later would be spam. A person can still send it.
        _record(lead, OutboundMessage.INITIAL, Channel.EMAIL, "initial:email", OutboundMessage.SKIPPED,
                "lead antigo (lido ou importado mais de 24 h depois): mensagem inicial automática não enviada")
        return "lead antigo"
    values = variables(lead)
    planned = []
    with transaction.atomic():
        if config.send_email:
            planned.append(_plan(lead, OutboundMessage.INITIAL, Channel.EMAIL, "initial:email", forced_by=forced_by,
                                 subject=render(config.email_subject, values)[:200], body=render(config.email_body, values),
                                 priority=PRIORITY_FIRST_CONTACT))
        if config.send_whatsapp:
            if not config.whatsapp_template:
                planned.append(_record(lead, OutboundMessage.INITIAL, Channel.WHATSAPP, "initial:whatsapp",
                                       OutboundMessage.SKIPPED, "sem template aprovado do WhatsApp para iniciar a conversa"))
            else:
                planned.append(_plan(lead, OutboundMessage.INITIAL, Channel.WHATSAPP, "initial:whatsapp", forced_by=forced_by,
                                     template={"name": config.whatsapp_template, "language": config.whatsapp_language,
                                               "params": [values.get(p, "") for p in config.whatsapp_params]},
                                     priority=PRIORITY_FIRST_CONTACT))
    return [f"{m.channel}:{m.status}" for m in planned]


def on_substage_entered(lead, substage):
    """Called by crm.funnel when a lead enters a sub-stage (inside the move's
    transaction)."""
    if substage is None or not (substage.message_text or "").strip():
        return
    pk, sub = lead.pk, substage.pk
    transaction.on_commit(lambda: plan_substage.enqueue(pk, sub))


@task
def plan_substage(lead_id, substage_id, forced_by=""):
    from .models import Substage

    lead = Lead.objects.select_related("tenant", "contact").filter(pk=lead_id).first()
    substage = Substage.objects.filter(pk=substage_id).first()
    if lead is None or substage is None or not lead.tenant.chatwoot_account_id:
        return "nada a enviar"
    text = render(substage.message_text, variables(lead))
    channel = SOURCE_CHANNEL.get(lead.source, Channel.EMAIL)
    if lead.conversation_id:
        msg = _plan(lead, OutboundMessage.SUBSTAGE, channel, f"substage:{substage.pk}", forced_by=forced_by,
                    conversation_id=lead.conversation_id, body=text, priority=PRIORITY_ANSWER)
    else:
        msg = _plan(lead, OutboundMessage.SUBSTAGE, Channel.EMAIL, f"substage:{substage.pk}", forced_by=forced_by,
                    subject=substage.name, body=text, priority=PRIORITY_ANSWER)
    return f"{msg.channel}:{msg.status}"


DEFAULT_WAIT_MESSAGE = "Obrigado pela mensagem! Um atendente vai continuar o seu atendimento em breve."


def wait_message_text(tenant):
    return (tenant.wait_message or "").strip() or DEFAULT_WAIT_MESSAGE


@task
def plan_wait(lead_id):
    """Once per human review, to a lead waiting for an answer in a
    conversation (decision of 2026-10-12). Channel rules apply (no free text
    outside WhatsApp's 24-hour window)."""
    lead = Lead.objects.select_related("tenant", "contact").filter(pk=lead_id).first()
    if lead is None or not lead.frozen_at or not lead.conversation_id or not lead.waiting_since:
        return "nada a enviar"
    tenant = lead.tenant
    if not tenant.ai_enabled_at or not tenant.wait_message_enabled or not tenant.chatwoot_account_id:
        return "mensagem de espera desligada"
    msg = _plan(lead, OutboundMessage.WAIT, SOURCE_CHANNEL.get(lead.source, Channel.EMAIL),
                f"wait:{int(lead.frozen_at.timestamp())}", conversation_id=lead.conversation_id,
                body=render(wait_message_text(tenant), variables(lead)), priority=PRIORITY_ANSWER)
    return f"{msg.channel}:{msg.status}"


def resend(message, actor, reason):
    """A person sends the same message again on purpose (reason recorded)."""
    lead = message.lead
    LeadEvent.objects.create(lead=lead, tenant=lead.tenant, type=EventType.NOTE, actor=actor,
                             reason=f"Reenvio de propósito: {reason}".strip(), data={"outbound": str(message.uuid)})
    with transaction.atomic():
        return _plan(lead, message.kind, message.channel, message.dedupe_key, forced_by=actor,
                     conversation_id=message.conversation_id or lead.conversation_id, subject=message.subject,
                     body=message.body, template=message.template, priority=PRIORITY_ANSWER)


# ── E-mail limits ────────────────────────────────────────────────────────────

def _daily_limit(tenant, inbox_id, now):
    first = (OutboundMessage.objects.filter(tenant=tenant, inbox_id=inbox_id, channel=Channel.EMAIL, status=OutboundMessage.SENT)
             .order_by("sent_at").values_list("sent_at", flat=True).first())
    age = (now - first).days if first else 0
    for days, limit in WARMUP:
        if age < days:
            return min(limit, tenant.email_daily_cap)
    return tenant.email_daily_cap


def email_slot(tenant, inbox_id, now=None):
    """None when an e-mail may go now, else when to try again."""
    now = now or timezone.now()
    sent = OutboundMessage.objects.filter(tenant=tenant, inbox_id=inbox_id, channel=Channel.EMAIL, status=OutboundMessage.SENT)
    local = timezone.localtime(now)
    day_start = timezone.make_aware(datetime.combine(local.date(), time(0, 0)), local.tzinfo)
    if sent.filter(sent_at__gte=day_start).count() >= _daily_limit(tenant, inbox_id, now):
        return timezone.make_aware(datetime.combine(local.date() + timedelta(days=1), time(8, 0)), local.tzinfo)
    last_hour = sent.filter(sent_at__gte=now - timedelta(hours=1)).order_by("sent_at")
    if last_hour.count() >= tenant.email_hourly_cap:
        return last_hour.first().sent_at + timedelta(hours=1)
    last = sent.order_by("-sent_at").values_list("sent_at", flat=True).first()
    gap = timedelta(seconds=3600 / max(tenant.email_hourly_cap, 1))
    if last and now - last < gap:
        return last + gap
    return None


# ── E-mail body ──────────────────────────────────────────────────────────────

URL_RE = re.compile(r"https?://[^\s<>\"']+")


def public_url(path):
    return f"{settings.SITE_URL.rstrip('/')}{path}"


def email_html(message):
    """The e-mail as HTML: paragraphs, every link rewritten to /t/<token>
    (clicks), an unsubscribe link and the open pixel."""
    def tracked(url):
        token = secrets.token_urlsafe(16)
        TrackedLink.objects.create(token=token, message=message, url=url)
        return f'<a href="{public_url(f"/t/{token}/")}">{html.escape(url)}</a>'

    paragraphs = []
    for block in re.split(r"\n\s*\n", (message.body or "").strip()):
        parts, last = [], 0
        for match in URL_RE.finditer(block):
            parts.append(html.escape(block[last:match.start()]))
            parts.append(tracked(match.group(0)))
            last = match.end()
        parts.append(html.escape(block[last:]))
        paragraphs.append("<p>" + "".join(parts).replace("\n", "<br>") + "</p>")
    unsubscribe = public_url(f"/descadastro/{message.uuid}/")
    footer = (f'<p style="font-size:12px;color:#6b7280">Não quer mais receber nossos e-mails? '
              f'<a href="{unsubscribe}">Descadastre-se</a>.</p>')
    pixel = f'<img src="{public_url(f"/o/{message.uuid}.gif")}" width="1" height="1" alt="" style="display:none">'
    return "".join(paragraphs) + footer + pixel


def email_text(message):
    return f"{message.body}\n\n--\nNão quer mais receber nossos e-mails? {public_url(f'/descadastro/{message.uuid}/')}"


# ── Sending ──────────────────────────────────────────────────────────────────

def _contact_id(lead):
    response = requests.post(
        f"{settings.CORE_API_URL.rstrip('/')}/api/v1/tenants/{lead.tenant.uuid}/chatwoot/contatos",
        json={"name": lead.contact.name, "email": lead.contact.email, "phone": lead.contact.phone_e164},
        headers={"Authorization": f"Bearer {settings.CORE_WEBHOOK_SECRET}"}, timeout=20,
    )
    if response.status_code != 200:
        raise ChatwootUnavailable(f"core: HTTP {response.status_code} {response.text[:200]}")
    return response.json()["contact_id"]


def _message_payload(message):
    if message.channel == Channel.EMAIL:
        return {"content": email_text(message), "message_type": "outgoing", "email_html_content": email_html(message)}
    payload = {"content": message.body or message.template.get("name", ""), "message_type": "outgoing"}
    if message.template:
        payload["template_params"] = {
            "name": message.template["name"], "language": message.template.get("language", "pt_BR"),
            "processed_params": {str(i + 1): v for i, v in enumerate(message.template.get("params", []))},
        }
    return payload


def _skip(message, reason):
    message.status, message.reason = OutboundMessage.SKIPPED, reason
    message.save(update_fields=["status", "reason"])
    LeadEvent.objects.create(lead=message.lead, tenant=message.tenant, type=EventType.MESSAGE_SKIPPED, actor="sistema",
                             reason=reason, data={"outbound": str(message.uuid), "kind": message.kind, "channel": message.channel})
    return "não enviada"


def send(message, now=None):
    """Sends one queued message; returns what happened."""
    now = now or timezone.now()
    tenant, lead = message.tenant, message.lead
    if message.channel in (Channel.EMAIL, Channel.WHATSAPP) and is_suppressed(tenant, address_of(lead, message.channel)):
        return _skip(message, "endereço na lista de bloqueio")
    if message.channel == Channel.EMAIL and message.inbox_id:
        later = email_slot(tenant, message.inbox_id, now)
        if later:
            message.send_after = later
            message.save(update_fields=["send_after"])
            return "aguardando o limite de e-mail"
    if message.conversation_id:
        conversation = bot_api(tenant, "GET", f"conversations/{message.conversation_id}")
        if message.channel in WINDOW_CHANNELS and conversation.get("can_reply") is False:
            if not message.template:
                return _skip(message, "fora da janela de 24 h do canal (só template ou atendente)")
        answer = bot_api(tenant, "POST", f"conversations/{message.conversation_id}/messages", _message_payload(message))
        conversation_id = message.conversation_id
    else:
        body = {"inbox_id": message.inbox_id, "contact_id": _contact_id(lead), "status": "open",
                "message": _message_payload(message)}
        if message.channel == Channel.EMAIL:
            body["additional_attributes"] = {"mail_subject": message.subject or DEFAULT_SUBJECT}
        answer = bot_api(tenant, "POST", "conversations", body)
        conversation_id = answer.get("id")
        if conversation_id and not lead.conversation_id:
            Lead.objects.filter(pk=lead.pk, conversation_id__isnull=True).update(conversation_id=conversation_id)
    message.status, message.sent_at, message.conversation_id = OutboundMessage.SENT, now, conversation_id
    messages = answer.get("messages") or []
    message.chatwoot_message_id = answer.get("id") if "content" in answer else (messages[-1].get("id") if messages else None)
    message.save(update_fields=["status", "sent_at", "conversation_id", "chatwoot_message_id"])
    LeadEvent.objects.create(lead=lead, tenant=tenant, type=EventType.MESSAGE_SENT,
                             actor=message.forced_by or "sistema",
                             data={"outbound": str(message.uuid), "kind": message.kind, "channel": message.channel,
                                   "conversation_id": conversation_id})
    return "enviada"


def process_due(now=None, limit=50):
    now = now or timezone.now()
    done = 0
    due = (OutboundMessage.objects.select_related("tenant", "lead__contact", "lead__tenant")
           .filter(status=OutboundMessage.QUEUED, send_after__lte=now)
           # Paused e-mail waits in the queue until a person resumes it.
           .exclude(channel=Channel.EMAIL, tenant__email_paused_at__isnull=False)
           .order_by("priority", "send_after", "id")[:limit])
    for message in due:
        try:
            with transaction.atomic():
                send(message, now)
        except (ChatwootUnavailable, requests.RequestException, KeyError) as exc:
            message.attempts += 1
            message.reason = str(exc)[:1000]
            if message.attempts >= MAX_ATTEMPTS:
                message.status = OutboundMessage.FAILED
            else:
                message.send_after = now + timedelta(minutes=5 * 2 ** (message.attempts - 1))
            message.save(update_fields=["attempts", "reason", "status", "send_after"])
            logger.warning("Mensagem %s não enviada: %s", message.uuid, exc)
        done += 1
    return done


def schedule_queue():
    if not DBTaskResult.objects.filter(task_path=QUEUE_PATH, status="READY", run_after__lte=timezone.now() + timedelta(seconds=5)).exists():
        process_queue.enqueue()


@task
def process_queue(reschedule=False):
    try:
        return process_due()
    finally:
        if reschedule:
            process_queue.using(run_after=timezone.now() + QUEUE_EVERY).enqueue(True)


def ensure_queue_loop():
    if DBTaskResult.objects.filter(task_path=QUEUE_PATH, status__in=["READY", "RUNNING"], args_kwargs__args=[True]).exists():
        return False
    process_queue.enqueue(True)
    return True


# ── Bounces seen in the e-mail inbox ─────────────────────────────────────────

BOUNCE_SENDERS = ("mailer-daemon", "postmaster")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def is_bounce(sender_email):
    local = (sender_email or "").split("@")[0].lower()
    return local in BOUNCE_SENDERS


def record_bounce(tenant, content):
    """A delivery failure notice: the addresses in it that the CRM e-mailed
    in the last 7 days go to the suppression list. Returns them."""
    since = timezone.now() - timedelta(days=7)
    found = []
    for address in {a.lower().rstrip(".") for a in EMAIL_RE.findall(content or "")}:
        sent = (OutboundMessage.objects.select_related("lead")
                .filter(tenant=tenant, channel=Channel.EMAIL, lead__contact__email=address, sent_at__gte=since).first())
        if sent:
            Suppression.objects.get_or_create(tenant=tenant, address=address, defaults={"reason": Suppression.BOUNCE})
            LeadEvent.objects.create(lead=sent.lead, tenant=tenant, type=EventType.MESSAGE_SKIPPED, actor="sistema",
                                     reason=f"E-mail devolvido: {address} bloqueado para envios")
            found.append(address)
    if found:
        check_email_health(tenant)
    return found


# ── Mailbox health: automatic pause ──────────────────────────────────────────

def email_health(tenant, now=None):
    now = now or timezone.now()
    since = max(now - HEALTH_WINDOW, tenant.email_resumed_at or now - HEALTH_WINDOW)
    sent = OutboundMessage.objects.filter(tenant=tenant, channel=Channel.EMAIL, status=OutboundMessage.SENT,
                                          sent_at__gte=since).count()
    marks = Suppression.objects.filter(tenant=tenant, created_at__gte=since)
    return {"sent": sent, "bounces": marks.filter(reason=Suppression.BOUNCE).count(),
            "complaints": marks.filter(reason=Suppression.COMPLAINT).count()}


def check_email_health(tenant, now=None):
    """Pauses the tenant's automatic e-mail when the mailbox looks unhealthy
    and tells the agency. Returns the reason, or "" when healthy."""
    if tenant.email_paused_at:
        return tenant.email_pause_reason
    h = email_health(tenant, now)
    reason = ""
    if h["sent"] >= HEALTH_MIN_SENT and h["bounces"] / h["sent"] >= HEALTH_MAX_BOUNCE_RATE:
        reason = (f"{h['bounces']} de {h['sent']} e-mails voltaram nos últimos 7 dias "
                  f"({100 * h['bounces'] // h['sent']}%, limite {int(HEALTH_MAX_BOUNCE_RATE * 100)}%).")
    elif h["complaints"] >= HEALTH_MAX_COMPLAINTS:
        reason = f"{h['complaints']} reclamações de destinatários nos últimos 7 dias (limite {HEALTH_MAX_COMPLAINTS})."
    if reason:
        Tenant.objects.filter(pk=tenant.pk).update(email_paused_at=now or timezone.now(), email_pause_reason=reason)
        tenant.email_paused_at, tenant.email_pause_reason = now or timezone.now(), reason
        pk = tenant.pk
        transaction.on_commit(lambda: alert_agency.enqueue(pk, "Envio automático de e-mail pausado", (
            f"O CRM pausou o envio automático de e-mail de {tenant.name}: {reason}\n\n"
            "Todos os clientes da Agitare enviam pelo mesmo servidor; uma caixa com muitas devoluções ou reclamações "
            "pode prejudicar a entrega de todos. Confira a lista de contatos e retome o envio na tela Mensagens do CRM. "
            "As mensagens ficaram na fila, nada foi perdido.")))
    return reason


def resume_email(tenant):
    Tenant.objects.filter(pk=tenant.pk).update(email_paused_at=None, email_pause_reason="", email_resumed_at=timezone.now())
    transaction.on_commit(schedule_queue)


@task
def alert_agency(tenant_id, subject, body):
    """The core knows who is responsible for the client and sends the e-mail."""
    tenant = Tenant.objects.filter(pk=tenant_id).first()
    if tenant is None or not settings.CORE_API_URL:
        return "sem core"
    response = requests.post(
        f"{settings.CORE_API_URL.rstrip('/')}/api/v1/tenants/{tenant.uuid}/avisos",
        json={"subject": subject, "body": body},
        headers={"Authorization": f"Bearer {settings.CORE_WEBHOOK_SECRET}"}, timeout=20,
    )
    response.raise_for_status()
    return response.json()
