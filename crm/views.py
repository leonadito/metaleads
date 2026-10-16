"""CRM screens (Phase 4): kanban, lead page and manual entry, on the CRM
tables (the old kanban read a Google Sheet tab). HTMX + Tailwind + Alpine.

The tenant is the client of the signed-in account (UserProfile.core_client_uuid
= Tenant.uuid); every query filters by it. Agency staff reach their clients
through the client selector, which signs into the client's account.
"""
import json
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import Count, Prefetch, Q
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import chatwoot_actions, funnel, summary
from .ingest import IngestError, ingest
from .models import CLOSED_STAGES, EventType, Lead, LeadAttribution, LeadEvent, Source, Stage, Substage, Tenant

COLUMN_LIMIT = 100
PERIODS = [("", "Todo o período"), ("7", "Últimos 7 dias"), ("30", "Últimos 30 dias"), ("90", "Últimos 90 dias")]
STAGE_COLORS = {
    Stage.CRIADO: "border-sky-400",
    Stage.EM_ANALISE: "border-amber-400",
    Stage.QUALIFICADO: "border-emerald-500",
    Stage.NAO_QUALIFICADO: "border-gray-400",
    Stage.CONVERTIDO: "border-brand",
    Stage.PERDIDO: "border-accent",
}


def current_tenant(request):
    profile = getattr(request.user, "profile", None)
    uuid = getattr(profile, "core_client_uuid", None)
    return Tenant.objects.filter(uuid=uuid).first() if uuid else None


def tenant_required(view):
    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        tenant = current_tenant(request)
        if tenant is None:
            return render(request, "crm/no_tenant.html", status=200)
        request.tenant = tenant
        return view(request, *args, **kwargs)

    return wrapper


def actor(request):
    user = request.user
    return (user.get_full_name() or user.email or user.get_username())[:200]


def toast(response, message):
    response["HX-Trigger"] = json.dumps({"crm-toast": message})
    return response


# ── Kanban ───────────────────────────────────────────────────────────────────

def _filters(request):
    return {
        "q": request.GET.get("q", "").strip()[:100],
        "source": request.GET.get("source", ""),
        "period": request.GET.get("period", ""),
        "substage": request.GET.get("substage", ""),
        "review": request.GET.get("review", ""),
        "waiting": request.GET.get("waiting", ""),
    }


def _filtered(tenant, f):
    qs = Lead.objects.filter(tenant=tenant)
    if f["q"]:
        digits = "".join(ch for ch in f["q"] if ch.isdigit())
        cond = Q(contact__name__icontains=f["q"]) | Q(contact__email__icontains=f["q"])
        if len(digits) >= 4:
            cond |= Q(contact__phone_e164__contains=digits)
        qs = qs.filter(cond)
    if f["source"] in Source.values:
        qs = qs.filter(source=f["source"])
    if f["period"] in ("7", "30", "90"):
        qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=int(f["period"])))
    if f["substage"].isdigit():
        qs = qs.filter(substage_id=int(f["substage"]))
    if f["review"] == "1":
        qs = qs.filter(frozen_at__isnull=False)
    if f["waiting"] == "1":
        qs = qs.filter(waiting_since__isnull=False)
    return qs


def _origin(lead):
    """Short "where from" line of a card: page, form or campaign."""
    attributions = list(lead.attributions.all())
    first = attributions[0] if attributions else None
    if lead.landing_page_name:
        return lead.landing_page_name
    if first:
        return first.form_name or first.campaign_name or first.ad_name or first.utm_campaign or ""
    return ""


def _board(request, tenant):
    f = _filters(request)
    qs = _filtered(tenant, f)
    counts = dict(qs.values_list("stage").annotate(n=Count("id")).values_list("stage", "n"))
    cards = (
        qs.select_related("contact", "substage")
        .prefetch_related(Prefetch("attributions", queryset=LeadAttribution.objects.order_by("occurred_at")))
        .order_by("-created_at")
    )
    columns = []
    for value, label in Stage.choices:
        leads = list(cards.filter(stage=value)[:COLUMN_LIMIT])
        for lead in leads:
            lead.origin = _origin(lead)
        columns.append({
            "value": value, "label": label, "leads": leads, "count": counts.get(value, 0),
            "color": STAGE_COLORS[value], "closed": value in CLOSED_STAGES,
        })
    return {"columns": columns, "filters": f, "column_limit": COLUMN_LIMIT}


@tenant_required
def kanban(request):
    context = _board(request, request.tenant)
    if request.headers.get("HX-Request"):
        return render(request, "crm/_board.html", context)
    context.update(
        sources=Source.choices, periods=PERIODS,
        substages=Substage.objects.filter(tenant=request.tenant).order_by("stage", "order", "id"),
        review_count=Lead.objects.filter(tenant=request.tenant, frozen_at__isnull=False).count(),
        waiting_count=Lead.objects.filter(tenant=request.tenant, waiting_since__isnull=False).count(),
    )
    return render(request, "crm/kanban.html", context)


# ── Lead page ────────────────────────────────────────────────────────────────

def _lead(request, lead_uuid):
    try:
        return Lead.objects.select_related("contact", "tenant").get(tenant=request.tenant, uuid=lead_uuid)
    except (Lead.DoesNotExist, ValueError):
        raise Http404("Lead não encontrado.")


def _label(key, labels):
    if key in labels:
        return labels[key]
    text = key.replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def _answers(lead, attributions):
    labels = {}
    for a in attributions:
        labels.update((a.raw or {}).get("form_labels") or {})
    out = []
    for key, value in (lead.form_answers or {}).items():
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        elif isinstance(value, bool):
            value = "Sim" if value else "Não"
        out.append((_label(key, labels), value))
    return out


def _chatwoot_url(lead):
    base = getattr(settings, "CHATWOOT_URL", "")
    account = lead.tenant.chatwoot_account_id
    if base and account and lead.conversation_id:
        return f"{base.rstrip('/')}/app/accounts/{account}/conversations/{lead.conversation_id}"
    return ""


def _whatsapp_url(phone):
    return f"https://wa.me/{phone.lstrip('+')}" if phone else ""


EVENT_COLORS = {
    EventType.CREATED: "bg-sky-500", EventType.REENTRY: "bg-sky-300", EventType.STAGE_CHANGED: "bg-brand",
    EventType.MESSAGE: "bg-emerald-400", EventType.NOTE: "bg-amber-400",
    EventType.REVIEW: "bg-accent", EventType.RELEASED: "bg-accent", EventType.AI_DECISION: "bg-indigo-400",
    EventType.MESSAGE_SENT: "bg-emerald-600", EventType.MESSAGE_SKIPPED: "bg-gray-400",
    EventType.UNSUBSCRIBED: "bg-accent",
    EventType.EMAIL_OPENED: "bg-gray-400", EventType.EMAIL_CLICKED: "bg-gray-400",
}


def _where(stages, stage, substage):
    label = stages.get(stage, stage)
    return f"{label} > {substage}" if substage else label


def _history(lead):
    stages = dict(Stage.choices)
    sources = dict(Source.choices)
    items = []
    for e in lead.events.all().order_by("-created_at"):
        d = e.data or {}
        if e.type == EventType.STAGE_CHANGED:
            text = f"{_where(stages, d.get('from'), d.get('from_substage'))} → {_where(stages, d.get('to'), d.get('to_substage'))}"
            if d.get("by") == funnel.AUTO:
                text += " (automático)"
        elif e.type in (EventType.CREATED, EventType.REENTRY):
            text = f"{e.get_type_display()} por {sources.get(d.get('source'), d.get('source') or '')}"
        elif e.type == EventType.AI_DECISION:
            dec = d.get("decision") or {}
            conf = dec.get("confianca")
            text = f"IA: {d.get('outcome', '')}" + (f" (confiança {float(conf):.2f})" if isinstance(conf, (int, float)) else "")
            if d.get("self_review"):
                text += " · autocrítica " + ("aprovou" if d["self_review"][-1].get("aprovada") else "reprovou")
        elif e.type == EventType.MESSAGE:
            text = "Mensagem do contato" if d.get("direction") == "incoming" else "Resposta do atendimento"
        else:
            text = e.get_type_display()
        items.append({"event": e, "color": EVENT_COLORS.get(e.type, "bg-gray-300"), "text": text})
    return items


def destinations(tenant):
    """Choices for "move to": every active sub-stage, grouped by stage; a
    stage with no active sub-stage is a choice by itself."""
    subs = {}
    for s in funnel.active_substages(tenant).order_by("order", "id"):
        subs.setdefault(s.stage, []).append((f"sub:{s.pk}", s.name))
    return [(label, subs.get(value) or [(f"stage:{value}", label)]) for value, label in Stage.choices]


def _parse_destination(value):
    kind, _, ref = (value or "").partition(":")
    if kind == "sub" and ref.isdigit():
        return None, int(ref)
    if kind == "stage":
        return ref, None
    return value, None  # plain stage (kanban)


def _detail_context(request, lead):
    attributions = list(lead.attributions.all().order_by("-occurred_at"))
    current = f"sub:{lead.substage_id}" if lead.substage_id else f"stage:{lead.stage}"
    return {
        "destinations": destinations(lead.tenant),
        "current_destination": current,
        "lead": lead,
        "contact": lead.contact,
        "answers": _answers(lead, attributions),
        "attributions": attributions,
        "history": _history(lead),
        "summary_groups": summary.groups(lead),
        "stages": Stage.choices,
        "chatwoot_url": _chatwoot_url(lead),
        "outbound": lead.outbound_messages.all()[:20],
        "whatsapp_url": _whatsapp_url(lead.contact.phone_e164),
    }


@tenant_required
def lead_detail(request, lead_uuid):
    lead = _lead(request, lead_uuid)
    return render(request, "crm/lead_detail.html", _detail_context(request, lead))


@tenant_required
@require_POST
def lead_stage(request, lead_uuid):
    lead = _lead(request, lead_uuid)
    stage, substage = _parse_destination(request.POST.get("destination") or request.POST.get("stage", ""))
    message = ""
    try:
        result = funnel.move(lead, stage, substage, actor=actor(request),
                             reason=request.POST.get("reason", "").strip()[:1000])
        message = result.message
    except funnel.FunnelError:
        return HttpResponse("Destino inválido.", status=400)
    except IntegrityError:
        message = "Este contato já tem outro lead aberto: feche-o antes de reabrir este."
    if request.POST.get("from") == "kanban":
        response = HttpResponse("")  # 200, not 204: the HX-Trigger must always be read
        response["HX-Trigger"] = json.dumps({"board-changed": True, **({"crm-toast": message} if message else {})})
        return response
    lead.refresh_from_db()
    response = render(request, "crm/_lead_main.html", _detail_context(request, lead))
    return toast(response, message) if message else response


@tenant_required
@require_POST
def lead_release(request, lead_uuid):
    lead = _lead(request, lead_uuid)
    funnel.release(lead, actor=actor(request), reason=request.POST.get("reason", "").strip()[:1000])
    lead.refresh_from_db()
    return toast(render(request, "crm/_lead_main.html", _detail_context(request, lead)), "Lead liberado da revisão.")


@tenant_required
@require_POST
def lead_note(request, lead_uuid):
    lead = _lead(request, lead_uuid)
    text = request.POST.get("text", "").strip()[:5000]
    if text:
        LeadEvent.objects.create(lead=lead, tenant=lead.tenant, type=EventType.NOTE, actor=actor(request), reason=text)
    return render(request, "crm/_lead_main.html", _detail_context(request, lead))


# ── Manual entry ─────────────────────────────────────────────────────────────

@tenant_required
def lead_new(request):
    values = {k: request.POST.get(k, "").strip() for k in ("name", "phone", "email", "city", "note")}
    error = ""
    if request.method == "POST":
        try:
            result = ingest(request.tenant, {
                "source": Source.MANUAL,
                "name": values["name"], "phone": values["phone"], "email": values["email"], "city": values["city"],
            }, actor=actor(request))
        except IngestError as exc:
            error = "Informe ao menos telefone ou e-mail." if "ao menos" in str(exc) else str(exc)
        else:
            lead = result.lead
            if values["note"]:
                LeadEvent.objects.create(lead=lead, tenant=lead.tenant, type=EventType.NOTE,
                                         actor=actor(request), reason=values["note"][:5000])
            url = reverse("crm_lead", args=[lead.uuid])
            if not result.created:
                url += "?existente=1"
            return redirect(url)
    return render(request, "crm/lead_form.html", {"values": values, "error": error})


# ── Funnel settings (sub-stages) ─────────────────────────────────────────────

def _funnel_context(tenant, error=""):
    from .models import MAX_SUBSTAGES_PER_STAGE

    subs = {}
    for s in Substage.objects.filter(tenant=tenant).annotate(n=Count("leads")).order_by("order", "id"):
        subs.setdefault(s.stage, []).append(s)
    stages = []
    for value, label in Stage.choices:
        items = subs.get(value, [])
        active = sum(1 for s in items if s.is_active)
        stages.append((value, label, items, active, active < MAX_SUBSTAGES_PER_STAGE))
    profile = tenant.profile or {}
    masters = funnel.stage_prompts(tenant)
    stages = [(value, label, items, active, room, masters[value]) for value, label, items, active, room in stages]
    return {"stages": stages, "error": error, "tenant": tenant, "max_substages": MAX_SUBSTAGES_PER_STAGE,
            "profile": profile,
            "profile_url": f"{settings.CORE_API_URL.rstrip('/')}/clientes/{tenant.slug}/perfil/" if settings.CORE_API_URL else ""}


def _funnel_section(request, error=""):
    return render(request, "crm/_funnel.html", _funnel_context(request.tenant, error))


@tenant_required
def funnel_settings(request):
    return render(request, "crm/funnel.html", _funnel_context(request.tenant))


@tenant_required
@require_POST
def substage_save(request):
    """Creates (no id) or edits a sub-stage, as a person (crm.funnel holds the
    rules: limit per stage, unique names, prompt lock)."""
    data = request.POST
    locked = data.get("prompt_locked") == "on"
    try:
        with transaction.atomic():
            if data.get("id"):
                sub = Substage.objects.filter(tenant=request.tenant, pk=data["id"]).first()
                if sub is None:
                    raise Http404
                funnel.update_substage(sub, name=data.get("name", ""), prompt=data.get("prompt", ""),
                                       message_text=data.get("message_text", ""), prompt_locked=locked)
            else:
                if data.get("stage", "") not in Stage.values:
                    return HttpResponse("Etapa inválida.", status=400)
                funnel.create_substage(request.tenant, data["stage"], data.get("name", ""), prompt=data.get("prompt", ""),
                                       message_text=data.get("message_text", ""), prompt_locked=locked)
    except funnel.FunnelError as exc:
        return _funnel_section(request, str(exc))
    except IntegrityError:
        return _funnel_section(request, "Já existe uma subetapa com esse nome nesta etapa.")
    return toast(_funnel_section(request), "Subetapa salva.")


@tenant_required
@require_POST
def ai_switch(request):
    """Turn the AI on (profile complete; the LLM builds the funnel), ask for a
    new build (after the profile changed), or turn it off."""
    from .funnel_ai import build_funnel

    action = request.POST.get("action")
    tenant = request.tenant
    if action == "off":
        Tenant.objects.filter(pk=tenant.pk).update(ai_enabled_at=None)
        return redirect("crm_funnel")
    if not (tenant.profile or {}).get("ready_for_ai"):
        return _funnel_section(request, "Para ligar a IA, o perfil do cliente precisa estar completo.")
    if action == "on" and not tenant.ai_enabled_at:
        Tenant.objects.filter(pk=tenant.pk).update(ai_enabled_at=timezone.now(), ai_last_error="")
    if action in ("on", "rebuild"):
        transaction.on_commit(lambda: build_funnel.enqueue(tenant.pk))
    return redirect("crm_funnel")


@tenant_required
@require_POST
def stage_prompt_save(request):
    """Master prompt of a stage, as a person (empty = back to the generic text)."""
    try:
        funnel.set_stage_prompt(request.tenant, request.POST.get("stage", ""), request.POST.get("prompt", ""),
                                prompt_locked=request.POST.get("prompt_locked") == "on")
    except funnel.FunnelError as exc:
        return _funnel_section(request, str(exc))
    return toast(_funnel_section(request), "Prompt mestre salvo.")


@tenant_required
@require_POST
def substage_toggle(request, pk):
    sub = Substage.objects.filter(tenant=request.tenant, pk=pk).first()
    if sub is None:
        raise Http404
    try:
        funnel.set_substage_active(sub, not sub.is_active)
    except funnel.FunnelError as exc:
        return _funnel_section(request, str(exc))
    return toast(_funnel_section(request), "Subetapa reativada." if sub.is_active else
                 "Subetapa desativada: os leads que estão nela continuam lá até alguém movê-los.")


@tenant_required
@require_POST
def substage_reorder(request, pk):
    sub = Substage.objects.filter(tenant=request.tenant, pk=pk).first()
    if sub is None:
        raise Http404
    siblings = list(Substage.objects.filter(tenant=request.tenant, stage=sub.stage).order_by("order", "id"))
    i = siblings.index(sub)
    j = i - 1 if request.POST.get("dir") == "up" else i + 1
    if 0 <= j < len(siblings):
        siblings[i], siblings[j] = siblings[j], siblings[i]
        for order, s in enumerate(siblings):
            if s.order != order:
                Substage.objects.filter(pk=s.pk).update(order=order)
        chatwoot_actions.funnel_changed(request.tenant)
    return _funnel_section(request)


@tenant_required
@require_POST
def waiting_settings(request):
    try:
        minutes = int(request.POST.get("waiting_alert_minutes", ""))
    except ValueError:
        minutes = 0
    if not 1 <= minutes <= 1440:
        return toast(_funnel_section(request, "Tempo de aviso: de 1 a 1440 minutos."), "Valor inválido.")
    Tenant.objects.filter(pk=request.tenant.pk).update(waiting_alert_minutes=minutes)
    request.tenant.waiting_alert_minutes = minutes
    return toast(_funnel_section(request), "Tempo de aviso salvo.")
