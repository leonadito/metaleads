import uuid
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

import requests
from django.core import mail
from django.test import TestCase, override_settings

from . import orphans
from .ingest import ingest
from .models import Lead, OrphanReport, Stage, Tenant

CONFIGURED = dict(CORE_API_URL="https://core.example", CORE_WEBHOOK_SECRET="seg",
                  AGIPAGES_API_URL="https://pages.example", AGIPAGES_INTEGRITY_TOKEN="tok",
                  ADMINS=[("ops", "ops@agitare.example")])


def core_payload(tenant, **extra):
    return {"id": str(tenant.uuid), "name": tenant.name, "slug": tenant.slug, "status": tenant.status, **extra}


@override_settings(**CONFIGURED)
class OrphanCheckTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(uuid=uuid.uuid4(), name="Alberto", slug="alberto")
        self.page_ok, self.page_gone, self.page_draft = (str(uuid.uuid4()) for _ in range(3))
        for i, page in enumerate([self.page_ok, self.page_gone, self.page_draft]):
            ingest(self.tenant, {"source": "agipages_form", "phone": f"5199999{i:04d}",
                                 "landing_page": {"id": page, "name": f"Página {i}"}})
        self.agipages = {
            "pages": {
                self.page_ok: {"status": "published", "tenant": str(self.tenant.uuid)},
                self.page_draft: {"status": "draft", "tenant": str(self.tenant.uuid)},
            },
            "tenants": [{"slug": "alberto", "name": "Alberto", "core_uuid": str(self.tenant.uuid)}],
            "undelivered_leads": [],
        }
        self.core = [core_payload(self.tenant)]

    def run_with(self, core=None, agipages=None, core_error=None):
        core_mock = mock.patch.object(orphans, "fetch_core_tenants",
                                      side_effect=core_error, return_value=core if core is not None else self.core)
        pages_mock = mock.patch.object(orphans, "fetch_agipages", return_value=agipages or self.agipages)
        with core_mock, pages_mock as fetch_pages:
            report = orphans.run_check()
        return report, fetch_pages

    def kinds(self, report):
        return sorted(f["kind"] for f in report.findings)

    def test_pages_gone_or_unpublished(self):
        report, fetch_pages = self.run_with()
        self.assertEqual(sorted(fetch_pages.call_args.args[0]), sorted([self.page_ok, self.page_gone, self.page_draft]))
        self.assertEqual(self.kinds(report), ["page_missing", "page_unpublished"])
        missing = next(f for f in report.findings if f["kind"] == "page_missing")
        self.assertEqual((missing["ref"], missing["tenant"], missing["count"]), (self.page_gone, "alberto", 1))
        self.assertIn("Página 1", missing["message"])

    def test_page_of_another_tenant(self):
        self.agipages["pages"][self.page_ok]["tenant"] = str(uuid.uuid4())
        self.agipages["pages"][self.page_gone] = {"status": "published", "tenant": str(self.tenant.uuid)}
        self.agipages["pages"][self.page_draft]["status"] = "published"
        report, _ = self.run_with()
        self.assertEqual(self.kinds(report), ["page_other_tenant"])

    def test_core_side(self):
        ghost = Tenant.objects.create(uuid=uuid.uuid4(), name="Fantasma", slug="fantasma")
        core = [core_payload(self.tenant, status="inactive")]
        self.agipages["pages"] = {p: {"status": "published", "tenant": str(self.tenant.uuid)}
                                  for p in (self.page_ok, self.page_gone, self.page_draft)}
        report, _ = self.run_with(core=core)
        self.assertEqual(self.kinds(report), ["tenant_copy_out_of_date", "tenant_inactive_with_open_leads",
                                              "tenant_missing_in_core"])
        inactive = next(f for f in report.findings if f["kind"] == "tenant_inactive_with_open_leads")
        self.assertEqual(inactive["count"], 3)
        self.assertEqual(next(f for f in report.findings if f["kind"] == "tenant_missing_in_core")["ref"], str(ghost.uuid))

    def test_closed_leads_of_inactive_tenant_are_fine(self):
        Lead.objects.update(stage=Stage.PERDIDO)
        Tenant.objects.update(status="inactive")
        self.agipages["pages"] = {p: {"status": "published", "tenant": str(self.tenant.uuid)}
                                  for p in (self.page_ok, self.page_gone, self.page_draft)}
        report, _ = self.run_with(core=[core_payload(Tenant.objects.get())])
        self.assertEqual(report.findings, [])

    def test_agipages_tenants_and_undelivered_leads(self):
        self.agipages["tenants"] += [{"slug": "solto", "name": "Solto", "core_uuid": None},
                                     {"slug": "velho", "name": "Velho", "core_uuid": str(uuid.uuid4())}]
        self.agipages["undelivered_leads"] = [{"uuid": "abc", "tenant": "alberto", "attempts": 8, "error": "HTTP 422"}]
        report, _ = self.run_with()
        self.assertEqual(self.kinds(report), ["agipages_lead_undelivered", "agipages_tenant_unknown_in_core",
                                              "agipages_tenant_unlinked", "page_missing", "page_unpublished"])

    def test_report_alerts_and_survives_a_failed_check(self):
        report, _ = self.run_with(core_error=requests.ConnectionError("fora do ar"))
        self.assertEqual(OrphanReport.objects.get(), report)
        self.assertIn("core: fora do ar", report.errors[0])
        self.assertEqual(self.kinds(report), ["page_missing", "page_unpublished"])  # agipages still checked
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("2 achado(s)", mail.outbox[0].subject)

    def test_nothing_found_sends_no_alert(self):
        self.agipages["pages"] = {p: {"status": "published", "tenant": str(self.tenant.uuid)}
                                  for p in (self.page_ok, self.page_gone, self.page_draft)}
        report, _ = self.run_with()
        self.assertEqual((report.count, report.errors), (0, []))
        self.assertEqual(mail.outbox, [])

    @override_settings(AGIPAGES_API_URL="", CORE_API_URL="")
    def test_unconfigured_checks_are_reported_as_failures(self):
        report = orphans.run_check()
        self.assertEqual(len(report.errors), 2)

    def test_next_run_is_3am_local(self):
        sp = ZoneInfo("America/Sao_Paulo")
        self.assertEqual(orphans.next_run(datetime(2026, 10, 14, 2, 0, tzinfo=sp)), datetime(2026, 10, 14, 3, 0, tzinfo=sp))
        self.assertEqual(orphans.next_run(datetime(2026, 10, 14, 15, 0, tzinfo=sp)), datetime(2026, 10, 15, 3, 0, tzinfo=sp))
