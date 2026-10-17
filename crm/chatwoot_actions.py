"""What the CRM does inside Chatwoot (Phase 5), with the token of the
account's Agent Bot "Agitare CRM" (created by the core; Chatwoot 4.18 lets
a bot show a conversation, change its status, set its custom attributes,
write messages and private notes, assign it and set its labels).

- "Subetapa" conversation attribute: always shows the lead's position
  ("Etapa > Subetapa"); its options are the tenant's funnel, set by the core
  (POST /api/v1/tenants/<uuid>/chatwoot/subetapas) whenever the funnel
  changes. An agent changing it moves the lead as a person's move
  (conversation_updated; no back-and-forth lock, releases the lead).
- Human review: label "revisão", a private note with the reason and the
  recent path, and the conversation opened for people. The label leaves when
  a person releases or moves the lead.
- Waiting for a person: the lead's last message without an agent's answer
  for longer than the tenant's alert time gets the label
  "aguardando-resposta" and a private note mentioning the assignee (or the
  account administrator), repeated at most once per alert time; the label
  and the flag leave when an agent answers.
Everything runs in tasks: Chatwoot being slow or down never blocks a move.
"""
import logging
from datetime import timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django_tasks import task
from django_tasks_db.models import DBTaskResult

from .models import EventType, Lead, LeadEvent, Stage, Substage, Tenant

logger = logging.getLogger(__name__)

ATTRIBUTE = "subetapa"
REVIEW_LABEL = "revisão"
WAITING_LABEL = "aguardando-resposta"
WAITING_CHECK_EVERY = timedelta(minutes=5)
WAITING_CHECK_PATH = "crm.chatwoot_actions.waiting_check"


class ChatwootUnavailable(Exception):
    pass


# ── Credentials and the bot's API ────────────────────────────────────────────

def fetch_credentials(tenant):
    """Reads the account's webhook secret, bot token and administrator from
    the core and keeps them in the tenant copy. Returns the core's answer."""
    response = requests.get(
        f"{settings.CORE_API_URL.rstrip('/')}/api/v1/tenants/{tenant.uuid}/chatwoot",
        headers={"Authorization": f"Bearer {settings.CORE_WEBHOOK_SECRET}"},
        timeout=10,
    )
    if response.status_code != 200:
        return {}
    data = response.json()
    values = {
        "chatwoot_webhook_secret": data.get("webhook_secret") or "",
        "chatwoot_bot_token": data.get("bot_token") or "",
        "chatwoot_admin_id": data.get("admin_user_id"),
        "chatwoot_admin_name": (data.get("admin_name") or "")[:200],
    }
    Tenant.objects.filter(pk=tenant.pk).update(**values)
    for name, value in values.items():
        setattr(tenant, name, value)
    return data


def bot_api(tenant, method, path, payload=None):
    """Account API with the bot's token; reads the token again once when
    Chatwoot refuses it (first use, or the core recreated the bot)."""
    if not (settings.CHATWOOT_URL and tenant.chatwoot_account_id):
        raise ChatwootUnavailable("tenant sem conta no Chatwoot")
    for attempt in (1, 2):
        if not tenant.chatwoot_bot_token or attempt == 2:
            fetch_credentials(tenant)
        if not tenant.chatwoot_bot_token:
            raise ChatwootUnavailable("conta sem o bot do CRM (rode chatwoot_crm_setup no core)")
        response = requests.request(
            method,
            f"{settings.CHATWOOT_URL}/api/v1/accounts/{tenant.chatwoot_account_id}/{path.lstrip('/')}",
            json=payload,
            headers={"api_access_token": tenant.chatwoot_bot_token},
            timeout=15,
        )
        if response.status_code == 401 and attempt == 1:
            continue
        if not 200 <= response.status_code < 300:
            raise ChatwootUnavailable(f"{method} {path}: HTTP {response.status_code} {response.text[:200]}")
        return response.json() if response.content else {}


def _labels(tenant, conversation_id):
    answer = bot_api(tenant, "GET", f"conversations/{conversation_id}/labels")
    return list(answer.get("payload", []) if isinstance(answer, dict) else answer)


def change_labels(tenant, conversation_id, add=(), remove=()):
    """Chatwoot replaces the whole list, so read it first."""
    current = _labels(tenant, conversation_id)
    wanted = [lbl for lbl in current if lbl not in remove] + [lbl for lbl in add if lbl not in current]
    if wanted != current:
        bot_api(tenant, "POST", f"conversations/{conversation_id}/labels", {"labels": wanted})


def private_note(tenant, conversation_id, text):
    bot_api(tenant, "POST", f"conversations/{conversation_id}/messages",
            {"content": text, "message_type": "outgoing", "private": True})


def mention(user_id, name):
    return f"[@{name}](mention://user/{user_id}/{name})" if user_id else ""


# ── Positions and the "Subetapa" attribute ───────────────────────────────────

def position_label(stage, substage):
    label = Stage(stage).label
    return f"{label} > {substage.name}" if substage else label


def funnel_options(tenant):
    """The attribute's options: every active sub-stage, or the bare stage
    when it has none (the same choices as the lead page)."""
    subs = {}
    for s in Substage.objects.filter(tenant=tenant, is_active=True).order_by("order", "id"):
        subs.setdefault(s.stage, []).append(position_label(s.stage, s))
    options = []
    for value, label in Stage.choices:
        options += subs.get(value) or [label]
    return options


def destination_for(tenant, label):
    for s in Substage.objects.filter(tenant=tenant, is_active=True):
        if position_label(s.stage, s) == label:
            return s.stage, s
    for value, stage_label in Stage.choices:
        if stage_label == label and not Substage.objects.filter(tenant=tenant, stage=value, is_active=True).exists():
            return value, None
    return None


def _has_chatwoot(lead):
    return bool(lead.conversation_id and lead.tenant.chatwoot_account_id and settings.CHATWOOT_URL)


def after_move(lead, review=False, released=False):
    """Called by crm.funnel inside the move's transaction: queues the
    Chatwoot side for after the commit."""
    if not _has_chatwoot(lead):
        return
    pk = lead.pk
    transaction.on_commit(lambda: sync_lead.enqueue(pk, review, released))


@task
def sync_lead(lead_id, review=False, released=False):
    lead = Lead.objects.select_related("tenant", "substage").filter(pk=lead_id).first()
    if lead is None or not _has_chatwoot(lead):
        return "sem conversa"
    tenant, conversation = lead.tenant, lead.conversation_id
    bot_api(tenant, "POST", f"conversations/{conversation}/custom_attributes",
            {"custom_attributes": {ATTRIBUTE: position_label(lead.stage, lead.substage)}, "merge": True})
    if review:
        path = [
            position_label(e.data.get("to"), None) + (f" > {e.data['to_substage']}" if e.data.get("to_substage") else "")
            for e in LeadEvent.objects.filter(lead=lead, type=EventType.STAGE_CHANGED).order_by("-created_at")[:5]
        ][::-1]
        change_labels(tenant, conversation, add=[REVIEW_LABEL])
        private_note(tenant, conversation, "\n".join(filter(None, [
            "Lead em revisão humana. As mudanças automáticas estão paradas até alguém decidir.",
            f"Motivo: {lead.review_reason}" if lead.review_reason else "",
            f"Caminho recente: {' → '.join(path)}" if path else "",
            "Para decidir: mude o atributo Subetapa desta conversa, ou resolva a conversa.",
        ])))
        bot_api(tenant, "POST", f"conversations/{conversation}/toggle_status", {"status": "open"})
        from .messaging import plan_wait

        plan_wait.enqueue(lead.pk)
    elif released:
        change_labels(tenant, conversation, remove=[REVIEW_LABEL])
    return "ok"


def after_release(lead):
    if _has_chatwoot(lead):
        pk = lead.pk
        transaction.on_commit(lambda: sync_lead.enqueue(pk, False, True))


@task
def push_funnel_options(tenant_id):
    """Sends the tenant's funnel to the core, which sets the options of the
    "Subetapa" attribute as the account's administrator."""
    tenant = Tenant.objects.filter(pk=tenant_id).first()
    if tenant is None or not tenant.chatwoot_account_id or not settings.CORE_API_URL:
        return "sem Chatwoot"
    response = requests.post(
        f"{settings.CORE_API_URL.rstrip('/')}/api/v1/tenants/{tenant.uuid}/chatwoot/subetapas",
        json={"options": funnel_options(tenant)},
        headers={"Authorization": f"Bearer {settings.CORE_WEBHOOK_SECRET}"},
        timeout=20,
    )
    if response.status_code != 200:
        raise ChatwootUnavailable(f"core: HTTP {response.status_code} {response.text[:200]}")
    return "ok"


def funnel_changed(tenant):
    if tenant.chatwoot_account_id:
        pk = tenant.pk
        transaction.on_commit(lambda: push_funnel_options.enqueue(pk))


# ── Events coming from Chatwoot ──────────────────────────────────────────────

def _lead_for(tenant, conversation_id):
    return (Lead.objects.select_related("tenant", "substage")
            .filter(tenant=tenant, conversation_id=conversation_id).order_by("-created_at").first())


@task
def conversation_changed(tenant_id, payload):
    """conversation_updated / conversation_status_changed."""
    from . import funnel

    tenant = Tenant.objects.filter(pk=tenant_id).first()
    lead = _lead_for(tenant, payload.get("id")) if tenant else None
    if lead is None:
        return "conversa sem lead"
    if payload.get("event") == "conversation_status_changed":
        if payload.get("status") in ("resolved", "pending") and lead.waiting_since:
            clear_waiting(lead)
        if payload.get("status") == "resolved":
            from . import handoff

            return "resolvida: a IA lê a conversa" if handoff.on_resolved(lead) else "resolvida"
        return f"status {payload.get('status')}"

    change = next((c[ATTRIBUTE_CHANGE] for c in payload.get("changed_attributes") or [] if ATTRIBUTE_CHANGE in c), None)
    if not change:
        return "sem mudança de subetapa"
    current = (change.get("current_value") or {}).get(ATTRIBUTE)
    previous = (change.get("previous_value") or {}).get(ATTRIBUTE)
    if not current or current == previous or current == position_label(lead.stage, lead.substage):
        return "nada a mover"  # includes the echo of the CRM's own update
    destination = destination_for(tenant, current)
    if destination is None:
        logger.warning("Subetapa %r do Chatwoot não existe no funil de %s", current, tenant)
        return "subetapa desconhecida"
    assignee = ((payload.get("meta") or {}).get("assignee") or {}).get("name") or ""
    when = timezone.localtime().strftime("%d/%m/%Y %H:%M")
    result = funnel.move(
        lead, destination[0], destination[1], actor=f"Chatwoot{f' ({assignee})' if assignee else ''}",
        reason=f"Definido no Chatwoot em {when}" + (f"; conversa com {assignee}" if assignee else ""),
    )
    return result.status


ATTRIBUTE_CHANGE = "custom_attributes"


def message_seen(lead, direction, sender_type, at=None):
    """Called for every public message of a conversation with a lead: the
    lead's message starts the wait, an agent's answer ends it."""
    at = at or timezone.now()
    if direction == "incoming":
        if lead.waiting_since is None:
            Lead.objects.filter(pk=lead.pk, waiting_since__isnull=True).update(waiting_since=at)
    elif direction == "outgoing" and sender_type == "user":
        from . import handoff

        handoff.on_agent_reply(lead)
        if lead.waiting_since:
            clear_waiting(lead)


def clear_waiting(lead):
    alerted = lead.waiting_alerted_at is not None
    Lead.objects.filter(pk=lead.pk).update(waiting_since=None, waiting_alerted_at=None)
    lead.waiting_since = lead.waiting_alerted_at = None
    if alerted and _has_chatwoot(lead):
        pk = lead.pk
        transaction.on_commit(lambda: remove_waiting_label.enqueue(pk))


@task
def remove_waiting_label(lead_id):
    lead = Lead.objects.select_related("tenant").filter(pk=lead_id).first()
    if lead and _has_chatwoot(lead):
        change_labels(lead.tenant, lead.conversation_id, remove=[WAITING_LABEL])
    return "ok"


# ── Waiting for a person ─────────────────────────────────────────────────────

def due_waiting(now=None):
    now = now or timezone.now()
    out = []
    for lead in (Lead.objects.select_related("tenant")
                 .filter(waiting_since__isnull=False, conversation_id__isnull=False, tenant__chatwoot_account_id__isnull=False)):
        limit = timedelta(minutes=lead.tenant.waiting_alert_minutes or 15)
        if lead.waiting_since <= now - limit and (lead.waiting_alerted_at is None or lead.waiting_alerted_at <= now - limit):
            out.append(lead)
    return out


def alert_waiting(lead, now=None):
    now = now or timezone.now()
    tenant, conversation_id = lead.tenant, lead.conversation_id
    conversation = bot_api(tenant, "GET", f"conversations/{conversation_id}")
    status = conversation.get("status")
    if status in ("resolved", "pending"):  # closed, or with the bot (LLM): nobody to wait for
        clear_waiting(lead)
        return "sem espera"
    assignee = (conversation.get("meta") or {}).get("assignee") or {}
    who = mention(assignee.get("id"), assignee.get("name")) if assignee.get("id") else \
        mention(tenant.chatwoot_admin_id, tenant.chatwoot_admin_name)
    minutes = int((now - lead.waiting_since).total_seconds() // 60)
    change_labels(tenant, conversation_id, add=[WAITING_LABEL])
    private_note(tenant, conversation_id, f"Mensagem do lead sem resposta há {minutes} min. {who}".strip())
    Lead.objects.filter(pk=lead.pk).update(waiting_alerted_at=now)
    return "avisado"


@task
def waiting_check():
    """Every 5 minutes. Re-enqueues itself. Also gives back to the AI the
    conversations reopened after a resolution that no one answered."""
    from . import handoff

    try:
        try:
            handoff.return_expired()
        except Exception:  # never stop the waiting alerts
            logger.exception("Volta das conversas para a IA falhou")
        for lead in due_waiting():
            try:
                alert_waiting(lead)
            except (ChatwootUnavailable, requests.RequestException) as exc:
                logger.warning("Aviso de espera do lead %s não enviado: %s", lead.uuid, exc)
    finally:
        waiting_check.using(run_after=timezone.now() + WAITING_CHECK_EVERY).enqueue()


def ensure_waiting_check():
    if DBTaskResult.objects.filter(task_path=WAITING_CHECK_PATH, status__in=["READY", "RUNNING"]).exists():
        return False
    waiting_check.enqueue()
    return True
