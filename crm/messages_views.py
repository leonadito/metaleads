"""CRM screen "Mensagens" (Phase 6): limits, the initial message of each form
(or the tenant's default), the queue and the suppression list."""
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from . import messaging
from .models import InitialMessage, Lead, OutboundMessage, Source, Suppression, Tenant
from .views import actor, tenant_required

FORM_LABELS = {Source.AGIPAGES_FORM: "Página do agipages", Source.META_FORM: "Formulário da Meta",
               Source.GOOGLE_ADS_FORM: "Formulário do Google Ads"}


def known_forms(tenant):
    """Every form that already brought leads, with its own message or the
    default."""
    own = {(m.source, m.form_key): m for m in InitialMessage.objects.filter(tenant=tenant).exclude(form_key="")}
    seen = {}
    leads = (Lead.objects.filter(tenant=tenant, source__in=messaging.FORM_SOURCES)
             .prefetch_related("attributions").order_by("-created_at")[:2000])
    for lead in leads:
        source, key, label = messaging.form_of(lead)
        if key and (source, key) not in seen:
            seen[(source, key)] = {"source": source, "source_label": FORM_LABELS.get(source, source), "key": key,
                                   "label": label or key, "message": own.get((source, key))}
    for (source, key), message in own.items():
        seen.setdefault((source, key), {"source": source, "source_label": FORM_LABELS.get(source, source), "key": key,
                                        "label": message.form_label or key, "message": message})
    return sorted(seen.values(), key=lambda f: (f["source_label"], f["label"].lower()))


def _context(request, edit=None, error=""):
    tenant = request.tenant
    return {
        "tenant": tenant,
        "default": messaging.default_initial_message(tenant),
        "forms": known_forms(tenant),
        "edit": edit,
        "error": error,
        "queue": OutboundMessage.objects.filter(tenant=tenant).select_related("lead__contact")[:40],
        "suppressions": Suppression.objects.filter(tenant=tenant).order_by("-created_at")[:200],
        "inboxes": messaging.inboxes(tenant, refresh=True),
        "variables": messaging.VARIABLES,
        "health": messaging.email_health(tenant),
        "waiting_paused": OutboundMessage.objects.filter(tenant=tenant, channel="email", status="queued").count()
        if tenant.email_paused_at else 0,
    }


@tenant_required
def messages_page(request):
    edit = None
    source, key = request.GET.get("source", ""), request.GET.get("form", "")
    if key:
        edit = InitialMessage.objects.filter(tenant=request.tenant, source=source, form_key=key).first() or InitialMessage(
            tenant=request.tenant, source=source, form_key=key, form_label=request.GET.get("label", "")[:255],
            email_subject=messaging.DEFAULT_SUBJECT, email_body=messaging.DEFAULT_EMAIL,
            whatsapp_params=["primeiro_nome", "pagina"],
        )
    return render(request, "crm/messages.html", _context(request, edit))


@tenant_required
@require_POST
def limits_save(request):
    values = {}
    for field, low, high in (("resend_window_days", 0, 365), ("email_hourly_cap", 1, 500), ("email_daily_cap", 1, 5000)):
        try:
            value = int(request.POST.get(field, ""))
        except ValueError:
            value = -1
        if not low <= value <= high:
            return render(request, "crm/messages.html", _context(request, error="Valores fora do permitido."))
        values[field] = value
    Tenant.objects.filter(pk=request.tenant.pk).update(**values)
    return redirect("crm_messages")


@tenant_required
@require_POST
def initial_save(request):
    data = request.POST
    source, key = data.get("source", ""), data.get("form_key", "")
    if key and source not in messaging.FORM_SOURCES:
        return HttpResponse("Fonte inválida.", status=400)
    message = (InitialMessage.objects.filter(tenant=request.tenant, source=source, form_key=key).first()
               or InitialMessage(tenant=request.tenant, source=source, form_key=key))
    if data.get("use_default") == "on" and key:
        if message.pk:
            message.delete()
        return redirect("crm_messages")
    message.form_label = data.get("form_label", "")[:255] or message.form_label
    message.is_active = data.get("is_active") == "on"
    message.send_email = data.get("send_email") == "on"
    message.email_subject = data.get("email_subject", "").strip()[:200]
    message.email_body = data.get("email_body", "").strip()[:5000]
    message.send_whatsapp = data.get("send_whatsapp") == "on"
    message.whatsapp_template = data.get("whatsapp_template", "").strip()[:100]
    message.whatsapp_language = data.get("whatsapp_language", "pt_BR").strip()[:10] or "pt_BR"
    message.whatsapp_params = [p.strip() for p in data.get("whatsapp_params", "").split(",") if p.strip() in messaging.VARIABLES]
    if message.send_email and not (message.email_subject and message.email_body):
        return render(request, "crm/messages.html", _context(request, message, "E-mail ligado: preencha o assunto e o texto."))
    message.save()
    return redirect("crm_messages")


@tenant_required
@require_POST
def suppression_add(request):
    address = request.POST.get("address", "").strip().lower()[:255]
    reason = request.POST.get("reason") if request.POST.get("reason") == Suppression.COMPLAINT else Suppression.MANUAL
    if address:
        Suppression.objects.get_or_create(tenant=request.tenant, address=address, defaults={"reason": reason})
        if reason == Suppression.COMPLAINT:
            messaging.check_email_health(request.tenant)
    return redirect("crm_messages")


@tenant_required
@require_POST
def email_resume(request):
    messaging.resume_email(request.tenant)
    return redirect("crm_messages")


@tenant_required
@require_POST
def suppression_remove(request, pk):
    Suppression.objects.filter(tenant=request.tenant, pk=pk).delete()
    return redirect("crm_messages")


@tenant_required
@require_POST
def message_resend(request, message_uuid):
    message = OutboundMessage.objects.select_related("lead__contact", "lead__tenant").filter(
        tenant=request.tenant, uuid=message_uuid).first()
    if message is None:
        raise Http404
    reason = request.POST.get("reason", "").strip()[:500]
    if not reason:
        return redirect(f"/crm/leads/{message.lead.uuid}/?reenvio=sem-motivo")
    new = messaging.resend(message, actor(request), reason)
    return redirect(f"/crm/leads/{message.lead.uuid}/?reenvio={new.status}")
