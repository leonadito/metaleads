"""Nightly orphan check (Arquitetura > regras de relação entre apps).

Apps reference each other by UUID, with no foreign key between databases.
Every night this compares those references and writes an OrphanReport; it
only reports and alerts, it never fixes anything by itself.

It runs here because metaleads is the hub: it holds the core's tenant copy
and the leads that point at agipages pages. agipages answers a read-only
endpoint (POST /api/v1/integrity/), so its own references to the core are
checked from here too:
- core: local tenant copies the core no longer has; copies out of date;
  inactive tenants that still have open leads;
- agipages pages referenced by leads: gone, unpublished, or belonging to
  another tenant;
- agipages tenants not linked to the core, or linked to an unknown uuid;
- agipages form leads metaleads never confirmed.
"""
import logging
from datetime import datetime, time, timedelta

import requests
from django.conf import settings
from django.core.mail import mail_admins
from django.db.models import Count
from django.utils import timezone
from django_tasks import task
from django_tasks_db.models import DBTaskResult

from .core_sync import fetch_core_tenants
from .models import CLOSED_STAGES, Lead, OrphanReport, Tenant

logger = logging.getLogger(__name__)

NIGHTLY_PATH = "crm.orphans.orphan_check_nightly"
RUN_AT = time(3, 0)  # local time (America/Sao_Paulo)
KEEP_REPORTS = timedelta(days=180)


def _finding(kind, message, ref="", tenant="", count=None):
    item = {"kind": kind, "message": message, "ref": ref, "tenant": tenant}
    if count is not None:
        item["count"] = count
    return item


def check_core(core_tenants):
    findings = []
    core = {str(t["id"]): t for t in core_tenants}
    lead_counts = dict(Lead.objects.values_list("tenant").annotate(n=Count("id")).values_list("tenant", "n"))
    open_counts = dict(
        Lead.objects.exclude(stage__in=CLOSED_STAGES).values_list("tenant").annotate(n=Count("id")).values_list("tenant", "n")
    )
    for tenant in Tenant.objects.all():
        remote = core.get(str(tenant.uuid))
        if remote is None:
            findings.append(_finding(
                "tenant_missing_in_core", f"Tenant {tenant.name} existe aqui, mas o core não o conhece.",
                ref=str(tenant.uuid), tenant=tenant.slug, count=lead_counts.get(tenant.pk, 0),
            ))
            continue
        if (remote.get("status") or "active") != tenant.status or (remote.get("name") or "") != tenant.name:
            findings.append(_finding(
                "tenant_copy_out_of_date",
                f"Cópia de {tenant.name} diferente do core (status/nome); a sincronização diária deve corrigir.",
                ref=str(tenant.uuid), tenant=tenant.slug,
            ))
        if (remote.get("status") or "active") != "active" and open_counts.get(tenant.pk):
            findings.append(_finding(
                "tenant_inactive_with_open_leads", f"Tenant {tenant.name} está inativo no core e tem leads abertos.",
                ref=str(tenant.uuid), tenant=tenant.slug, count=open_counts[tenant.pk],
            ))
    return findings


def fetch_agipages(page_uuids):
    response = requests.post(
        f"{settings.AGIPAGES_API_URL.rstrip('/')}/api/v1/integrity/",
        json={"pages": page_uuids},
        headers={"Authorization": f"Bearer {settings.AGIPAGES_INTEGRITY_TOKEN}"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def check_agipages(answer, core_tenants=None):
    findings = []
    pages = answer.get("pages") or {}
    referenced = (
        Lead.objects.filter(landing_page_uuid__isnull=False)
        .values("landing_page_uuid", "landing_page_name", "tenant__uuid", "tenant__slug")
        .annotate(n=Count("id"))
    )
    for row in referenced:
        uuid = str(row["landing_page_uuid"])
        page = pages.get(uuid)
        name = row["landing_page_name"] or uuid
        if page is None:
            findings.append(_finding("page_missing", f"Página {name} não existe mais no agipages.",
                                     ref=uuid, tenant=row["tenant__slug"], count=row["n"]))
            continue
        if page.get("tenant") and page["tenant"] != str(row["tenant__uuid"]):
            findings.append(_finding("page_other_tenant", f"Página {name} pertence a outro tenant no agipages.",
                                     ref=uuid, tenant=row["tenant__slug"], count=row["n"]))
        if page.get("status") != "published":
            findings.append(_finding("page_unpublished", f"Página {name} não está publicada no agipages.",
                                     ref=uuid, tenant=row["tenant__slug"], count=row["n"]))

    known = {str(t["id"]) for t in core_tenants} if core_tenants is not None else None
    for t in answer.get("tenants") or []:
        if not t.get("core_uuid"):
            findings.append(_finding("agipages_tenant_unlinked", f"Tenant {t['name']} do agipages não está ligado ao core.",
                                     tenant=t["slug"]))
        elif known is not None and t["core_uuid"] not in known:
            findings.append(_finding("agipages_tenant_unknown_in_core",
                                     f"Tenant {t['name']} do agipages aponta para um id que o core não conhece.",
                                     ref=t["core_uuid"], tenant=t["slug"]))
    for lead in answer.get("undelivered_leads") or []:
        findings.append(_finding(
            "agipages_lead_undelivered",
            f"Lead do agipages não chegou ao CRM ({lead.get('attempts')} tentativa(s)): {lead.get('error') or 'sem erro'}",
            ref=lead["uuid"], tenant=lead.get("tenant", ""),
        ))
    return findings


def run_check():
    findings, errors = [], []
    core_tenants = None
    if settings.CORE_API_URL and settings.CORE_WEBHOOK_SECRET:
        try:
            core_tenants = fetch_core_tenants()
            findings += check_core(core_tenants)
        except (requests.RequestException, KeyError, ValueError) as exc:
            errors.append(f"core: {exc}"[:500])
    else:
        errors.append("core: CORE_API_URL não configurada")
    if settings.AGIPAGES_API_URL and settings.AGIPAGES_INTEGRITY_TOKEN:
        uuids = sorted({str(u) for u in Lead.objects.filter(landing_page_uuid__isnull=False)
                        .values_list("landing_page_uuid", flat=True)})
        try:
            findings += check_agipages(fetch_agipages(uuids), core_tenants)
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"agipages: {exc}"[:500])
    else:
        errors.append("agipages: AGIPAGES_API_URL não configurada")
    report = OrphanReport.objects.create(findings=findings, count=len(findings), errors=errors)
    if findings or errors:
        _alert(report)
    return report


def _alert(report):
    lines = [f"- {f['message']} ({f['tenant'] or '-'} {f['ref']})".rstrip() for f in report.findings[:100]]
    lines += [f"- checagem falhou: {e}" for e in report.errors]
    logger.warning("Checagem de órfãos: %s achado(s), %s falha(s)", report.count, len(report.errors))
    try:
        mail_admins(f"[metaleads] checagem de órfãos: {report.count} achado(s)", "\n".join(lines), fail_silently=True)
    except Exception:  # the report is already saved; the alert must not break the task
        logger.exception("Alerta da checagem de órfãos não enviado")


def next_run(now=None):
    now = timezone.localtime(now or timezone.now())
    run = timezone.make_aware(datetime.combine(now.date(), RUN_AT), now.tzinfo)
    return run if run > now else run + timedelta(days=1)


@task
def orphan_check_nightly():
    try:
        report = run_check()
        OrphanReport.objects.filter(created_at__lt=timezone.now() - KEEP_REPORTS).delete()
        return {"achados": report.count, "falhas": len(report.errors)}
    finally:
        orphan_check_nightly.using(run_after=next_run()).enqueue()


def ensure_nightly_check():
    if DBTaskResult.objects.filter(task_path=NIGHTLY_PATH, status__in=["READY", "RUNNING"]).exists():
        return False
    orphan_check_nightly.using(run_after=next_run()).enqueue()
    return True
