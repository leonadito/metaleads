"""Moving leads through the funnel (Phases 4 and 5). Every move goes through
here: it writes the history (who, why, by a person or automatically) and
queues the stage for Meta's funnel when the lead came from a Meta form.

Free path (decision of 2026-10-12): any stage > sub-stage to any other. The
code only checks that the destination exists and is active in the tenant's
funnel, and records every change. Automatic moves (the LLM, Phase 7) also go
through the back-and-forth lock:
- going back to a position the lead left in the last 24 h (A → B → A) is not
  applied: the lead is frozen and goes to human review with the history;
- a frozen lead takes no automatic move until a person moves or releases it;
- a destination that does not exist (the LLM inventing a sub-stage) also
  sends the lead to review instead of raising.
Moves by people never trigger the lock, and they release a frozen lead.
"""
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from . import chatwoot_actions, meta
from .models import CLOSED_STAGES, EventType, Lead, LeadEvent, Stage, Substage

BACK_AND_FORTH_WINDOW = timedelta(hours=24)
HUMAN, AUTO = "human", "auto"


class FunnelError(ValueError):
    pass


@dataclass
class MoveResult:
    status: str  # "moved", "unchanged", "review", "frozen"
    lead: Lead
    message: str = ""

    @property
    def moved(self):
        return self.status == "moved"


def position(stage, substage_id):
    return f"{stage}:{substage_id or ''}"


def active_substages(tenant, stage=None):
    qs = Substage.objects.filter(tenant=tenant, is_active=True)
    return qs.filter(stage=stage) if stage else qs


def resolve(tenant, stage=None, substage=None):
    """(stage, substage) for a destination. A sub-stage must be the tenant's
    and active; a bare stage that has active sub-stages lands on the first."""
    if substage is not None:
        if not isinstance(substage, Substage):
            substage = Substage.objects.filter(pk=substage).first()
        if substage is None or substage.tenant_id != tenant.pk or not substage.is_active:
            raise FunnelError("subetapa inexistente ou desativada neste funil")
        return substage.stage, substage
    if stage not in Stage.values:
        raise FunnelError(f"etapa inválida: {stage}")
    return stage, active_substages(tenant, stage).order_by("order", "id").first()


def _label(stage, substage):
    return f"{Stage(stage).label} > {substage.name}" if substage else Stage(stage).label


def _send_to_review(lead, reason, actor, data):
    lead.frozen_at = timezone.now()
    lead.review_reason = reason
    lead.save(update_fields=["frozen_at", "review_reason", "updated_at"])
    LeadEvent.objects.create(lead=lead, tenant=lead.tenant, type=EventType.REVIEW, actor=actor, reason=reason, data=data)
    chatwoot_actions.after_move(lead, review=True)
    return MoveResult("review", lead, reason)


def move(lead, stage=None, substage=None, *, actor, reason="", by=HUMAN):
    """Moves the lead. `by` is HUMAN (admin panel, Chatwoot attribute) or
    AUTO (the LLM). Reopening a closed lead while the contact has another
    open one raises IntegrityError (one open lead per contact)."""
    with transaction.atomic():
        # of=self: Postgres cannot lock the nullable side of the substage join.
        lead = Lead.objects.select_for_update(of=("self",)).select_related("tenant", "substage").get(pk=lead.pk)
        try:
            stage, substage = resolve(lead.tenant, stage, substage)
        except FunnelError as exc:
            if by == AUTO:
                return _send_to_review(lead, f"Destino inválido: {exc}", actor, {"asked": {"stage": stage, "substage": str(substage)}})
            raise
        current = position(lead.stage, lead.substage_id)
        target = position(stage, substage.pk if substage else None)
        if by == AUTO and lead.frozen_at:
            return MoveResult("frozen", lead, "Lead em revisão humana: mudanças automáticas paradas.")
        if current == target:
            return MoveResult("unchanged", lead)
        if by == AUTO:
            since = timezone.now() - BACK_AND_FORTH_WINDOW
            left = LeadEvent.objects.filter(
                lead=lead, type=EventType.STAGE_CHANGED, created_at__gte=since
            ).values_list("data__from_position", flat=True)
            if target in set(left):
                return _send_to_review(
                    lead,
                    f"Vaivém: voltaria para {_label(stage, substage)}, de onde saiu nas últimas 24 h. "
                    f"Sugestão: {reason or 'sem motivo'}",
                    actor,
                    {"from_position": current, "suggested_position": target},
                )

        previous_stage, previous_sub = lead.stage, lead.substage
        now = timezone.now()
        lead.stage, lead.substage = stage, substage
        if previous_stage != stage:
            lead.closed_at = now if stage in CLOSED_STAGES else None
        released = by == HUMAN and lead.frozen_at is not None
        if released:
            lead.frozen_at, lead.review_reason = None, ""
        lead.save(update_fields=["stage", "substage", "closed_at", "frozen_at", "review_reason", "updated_at"])
        LeadEvent.objects.create(
            lead=lead,
            tenant=lead.tenant,
            type=EventType.STAGE_CHANGED,
            actor=actor,
            reason=reason,
            data={
                "from": previous_stage, "to": stage,
                "from_substage": previous_sub.name if previous_sub else "",
                "to_substage": substage.name if substage else "",
                "from_position": current, "to_position": target,
                "by": by, "released": released,
            },
        )
        if previous_stage != stage:
            meta.record_stage(lead, stage, now)
        chatwoot_actions.after_move(lead, released=released)
    return MoveResult("moved", lead, f"Movido para {_label(stage, substage)}.")


def release(lead, *, actor, reason=""):
    """A person takes the lead out of review without moving it."""
    with transaction.atomic():
        lead = Lead.objects.select_for_update().select_related("tenant").get(pk=lead.pk)
        if not lead.frozen_at:
            return False
        lead.frozen_at, lead.review_reason = None, ""
        lead.save(update_fields=["frozen_at", "review_reason", "updated_at"])
        LeadEvent.objects.create(lead=lead, tenant=lead.tenant, type=EventType.RELEASED, actor=actor, reason=reason)
        chatwoot_actions.after_release(lead)
    return True


def change_stage(lead, stage, actor, reason=""):
    """Stage-only move by a person (kept for the admin and older callers).
    Returns False when the lead already was there."""
    if stage not in Stage.values:
        raise ValueError(f"etapa inválida: {stage}")
    return move(lead, stage, actor=actor, reason=reason).moved
