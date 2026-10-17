"""Who holds the conversation after a person resolves it (Phase 7; decisions
of 2026-10-12, see the plan, "Atendimento humano pelo Chatwoot").

- Resolved by a person: the AI reads the whole conversation, the human part
  included, moves the lead by the normal rules (self-review on critical
  moves) and takes it out of human review. It does not answer: the
  conversation is closed.
- The lead writes again within WINDOW of the resolution: the conversation
  reopens to the people, counting WINDOW again from that message (only the
  first message after the resolution counts). A person's answer to the lead
  keeps it with the people; with no answer in time, it goes back to the AI,
  which answers the lead's waiting message. After WINDOW from the
  resolution, the AI takes it directly.
- This is the only case where a conversation goes back to the AI by time: one
  a person holds, or one in human review, stays with the people.
Only for tenants with the AI on; without it the people hold everything.
"""
import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django_tasks import task

from .models import Lead

logger = logging.getLogger(__name__)

WINDOW = timedelta(hours=1)


def with_people(lead, now=None):
    return bool(lead.human_window_until and lead.human_window_until > (now or timezone.now()))


def on_resolved(lead):
    """conversation_status_changed → resolved."""
    from . import routing

    now = timezone.now()
    Lead.objects.filter(pk=lead.pk).update(resolved_at=now, human_window_until=None)
    lead.resolved_at, lead.human_window_until = now, None
    if lead.tenant.ai_enabled_at and lead.conversation_id and lead.is_open:
        pk = lead.pk
        transaction.on_commit(lambda: routing.route_lead.enqueue(pk, True))
        return True
    return False


def on_lead_message(lead, now=None):
    """The lead wrote. True when the conversation goes to the people (written
    within WINDOW of a resolution)."""
    now = now or timezone.now()
    if not lead.resolved_at or not lead.tenant.ai_enabled_at:
        return False
    recent = now - lead.resolved_at < WINDOW
    until = now + WINDOW if recent else None
    Lead.objects.filter(pk=lead.pk).update(resolved_at=None, human_window_until=until)
    lead.resolved_at, lead.human_window_until = None, until
    if recent:
        pk = lead.pk
        transaction.on_commit(lambda: set_status.enqueue(pk, "open"))
    return recent


def on_agent_reply(lead):
    """A person answered the lead: the conversation stays with the people."""
    if lead.human_window_until:
        Lead.objects.filter(pk=lead.pk).update(human_window_until=None)
        lead.human_window_until = None


@task
def set_status(lead_id, status):
    from .chatwoot_actions import bot_api

    lead = Lead.objects.select_related("tenant").filter(pk=lead_id).first()
    if lead is None or not lead.conversation_id:
        return "sem conversa"
    bot_api(lead.tenant, "POST", f"conversations/{lead.conversation_id}/toggle_status", {"status": status})
    return status


def return_expired(now=None):
    """Conversations reopened to the people after a resolution where no one
    answered in time go back to the AI. Called every 5 minutes by
    chatwoot_actions.waiting_check."""
    from . import sdr

    now = now or timezone.now()
    count = 0
    for lead in Lead.objects.select_related("tenant").filter(human_window_until__lte=now):
        Lead.objects.filter(pk=lead.pk).update(human_window_until=None)
        lead.human_window_until = None
        if not lead.tenant.ai_enabled_at or lead.frozen_at:
            continue
        set_status.enqueue(lead.pk, "pending")
        sdr.schedule(lead)
        count += 1
    return count
