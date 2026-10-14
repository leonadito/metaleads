"""Stage changes of a lead. Every change goes through change_stage: it writes
the history (who and why) and queues the stage for Meta's funnel when the
lead came from a Meta form (crm/meta.py)."""
from django.db import transaction
from django.utils import timezone

from . import meta
from .models import CLOSED_STAGES, EventType, Lead, LeadEvent, Stage


def change_stage(lead, stage, actor, reason=""):
    """Moves the lead to `stage`. Returns False when it already was there.
    Reopening a closed lead while the contact has another open one raises
    IntegrityError (one open lead per contact)."""
    if stage not in Stage.values:
        raise ValueError(f"etapa inválida: {stage}")
    with transaction.atomic():
        lead = Lead.objects.select_for_update().get(pk=lead.pk)
        if lead.stage == stage:
            return False
        previous = lead.stage
        now = timezone.now()
        lead.stage = stage
        lead.closed_at = now if stage in CLOSED_STAGES else None
        lead.save(update_fields=["stage", "closed_at", "updated_at"])
        LeadEvent.objects.create(
            lead=lead,
            tenant=lead.tenant,
            type=EventType.STAGE_CHANGED,
            actor=actor,
            reason=reason,
            data={"from": previous, "to": stage},
        )
        meta.record_stage(lead, stage, now)
    return True
