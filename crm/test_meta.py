import hashlib
import hmac
import json
import uuid
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from . import meta
from .funnel import change_stage
from .models import Contact, EventType, Lead, LeadAttribution, LeadEvent, MetaCrmEvent, MetaPage, Stage, Tenant

APP_SECRET = "segredo-do-app"


def graph_lead(leadgen_id="1234567890123456", phone="+55 51 99999-8888", created=None, form_id="900", extra=None,
               email="ANA@EXEMPLO.COM"):
    created = created or timezone.now().strftime("%Y-%m-%dT%H:%M:%S+0000")
    return {
        "id": leadgen_id,
        "created_time": created,
        "field_data": [
            {"name": "full_name", "values": ["Ana Souza"]},
            {"name": "email", "values": [email]},
            {"name": "phone_number", "values": [phone]},
            {"name": "city", "values": ["Porto Alegre"]},
            {"name": "qual_o_imovel?", "values": ["Casa"]},
            {"name": "interesses", "values": ["compra", "aluguel"]},
            *(extra or []),
        ],
        "ad_id": "11", "ad_name": "Anúncio A",
        "adset_id": "22", "adset_name": "Conjunto B",
        "campaign_id": "33", "campaign_name": "Campanha C",
        "form_id": form_id, "is_organic": False, "platform": "ig",
    }


class FakeGraph:
    """Stands in for crm.meta.graph: answers by path, records the calls."""

    def __init__(self, leads=(), forms=(("900", "Form Casa"),), fail_events=None):
        self.leads = {lead["id"]: lead for lead in leads}
        self.forms = forms
        self.fail_events = fail_events  # MetaError raised on POST .../events
        self.calls = []

    def __call__(self, method, path, token=None, params=None, data=None):
        self.calls.append((method, path, params, data))
        if path == "555":
            return {"access_token": "token-da-pagina", "name": "Página Teste"}
        if path.endswith("/leadgen_forms"):
            return {"data": [{"id": i, "name": n} for i, n in self.forms]}
        if path.endswith("/leads"):
            return {"data": list(self.leads.values())}
        if path.endswith("/events"):
            if self.fail_events:
                raise self.fail_events(data)
            return {"events_received": len(json.loads(data["data"]))}
        if path in self.leads:
            return self.leads[path]
        if path == "900":
            return {"name": "Form Casa"}
        if path.endswith("/subscribed_apps"):
            return {"success": True}
        raise meta.MetaError(f"inesperado: {method} {path}", status=400, permanent=True)


@override_settings(
    META_APP_SECRET=APP_SECRET, META_VERIFY_TOKEN="verifica", META_ACCESS_TOKEN="token-sistema",
    META_CAPI_ENABLED=True, META_TEST_EVENT_CODE="", META_LEAD_EVENT_SOURCE="Agitare CRM",
)
class MetaTestBase(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(uuid=uuid.uuid4(), name="Cliente", slug="cliente")
        self.page = MetaPage.objects.create(tenant=self.tenant, page_id="555", dataset_id="777", send_events=True)

    def fake(self, **kwargs):
        fake = FakeGraph(**kwargs)
        patcher = mock.patch.object(meta, "graph", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake


class WebhookTests(MetaTestBase):
    def post(self, payload, secret=APP_SECRET):
        body = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return self.client.post("/webhooks/meta/", body, content_type="application/json", HTTP_X_HUB_SIGNATURE_256=sig)

    def leadgen(self, page_id="555"):
        return {"object": "page", "entry": [{"id": page_id, "time": 1, "changes": [{"field": "leadgen", "value": {
            "leadgen_id": "1234567890123456", "page_id": page_id, "form_id": "900", "ad_id": "11", "created_time": 1}}]}]}

    def test_verification(self):
        ok = self.client.get("/webhooks/meta/", {"hub.mode": "subscribe", "hub.verify_token": "verifica", "hub.challenge": "42"})
        self.assertEqual((ok.status_code, ok.content), (200, b"42"))
        bad = self.client.get("/webhooks/meta/", {"hub.mode": "subscribe", "hub.verify_token": "outro", "hub.challenge": "42"})
        self.assertEqual(bad.status_code, 403)

    def test_bad_signature_is_refused(self):
        self.assertEqual(self.post(self.leadgen(), secret="errado").status_code, 403)
        self.assertFalse(DBTaskResult.objects.exists())

    def test_leadgen_is_queued(self):
        self.assertEqual(self.post(self.leadgen()).status_code, 200)
        task = DBTaskResult.objects.get(task_path="crm.meta.fetch_lead")
        self.assertEqual(task.args_kwargs["args"][:2], [self.page.pk, "1234567890123456"])

    def test_unknown_page_is_acknowledged_but_ignored(self):
        self.assertEqual(self.post(self.leadgen(page_id="999")).status_code, 200)
        self.assertFalse(DBTaskResult.objects.exists())


class FetchTests(MetaTestBase):
    def test_lead_is_stored(self):
        self.fake(leads=[graph_lead()])
        meta.fetch_lead.call(self.page.pk, "1234567890123456", {"page_id": "555"})
        lead = Lead.objects.get()
        self.assertEqual((lead.source, lead.external_id, lead.stage), ("meta_form", "1234567890123456", Stage.CRIADO))
        self.assertEqual(lead.form_answers, {"qual_o_imovel?": "Casa", "interesses": ["compra", "aluguel"]})
        contact = lead.contact
        self.assertEqual((contact.name, contact.email, contact.phone_e164, contact.city),
                         ("Ana Souza", "ana@exemplo.com", "+5551999998888", "Porto Alegre"))
        a = LeadAttribution.objects.get()
        self.assertEqual((a.platform, a.kind, a.campaign_name, a.adset_name, a.ad_name, a.form_name, a.placement, a.is_organic),
                         ("meta", "form", "Campanha C", "Conjunto B", "Anúncio A", "Form Casa", "instagram", False))
        self.assertEqual(a.raw["page_id"], "555")
        self.assertEqual(a.raw["lead"]["id"], "1234567890123456")
        # Entry goes to Meta's funnel as "Criado".
        event = MetaCrmEvent.objects.get()
        self.assertEqual((event.meta_lead_id, event.event_name, event.dataset_id), ("1234567890123456", "Criado", "777"))

    def test_same_lead_twice_is_one_lead_one_event(self):
        self.fake(leads=[graph_lead()])
        meta.fetch_lead.call(self.page.pk, "1234567890123456")
        meta.fetch_lead.call(self.page.pk, "1234567890123456")
        self.assertEqual((Lead.objects.count(), MetaCrmEvent.objects.count()), (1, 1))

    def test_no_events_while_page_does_not_send(self):
        MetaPage.objects.filter(pk=self.page.pk).update(send_events=False)
        self.fake(leads=[graph_lead()])
        meta.fetch_lead.call(self.page.pk, "1234567890123456")
        self.assertEqual(Lead.objects.count(), 1)
        self.assertFalse(MetaCrmEvent.objects.exists())

    def test_late_lead_keeps_its_real_date(self):
        created = timezone.now() - timedelta(days=3)
        self.fake(leads=[graph_lead(created=created.strftime("%Y-%m-%dT%H:%M:%S+0000"))])
        meta.fetch_lead.call(self.page.pk, "1234567890123456")
        lead = Lead.objects.get()
        self.assertLess(abs((lead.created_at - created).total_seconds()), 1)
        self.assertLess(abs((MetaCrmEvent.objects.get().event_time - created).total_seconds()), 1)

    def test_second_form_of_open_lead_is_a_reentry_with_its_own_entry(self):
        self.fake(leads=[graph_lead(), graph_lead(leadgen_id="6543210987654321")])
        meta.fetch_lead.call(self.page.pk, "1234567890123456")
        meta.fetch_lead.call(self.page.pk, "6543210987654321")
        lead = Lead.objects.get()
        self.assertEqual(meta.meta_lead_ids(lead), ["1234567890123456", "6543210987654321"])
        self.assertEqual(sorted(MetaCrmEvent.objects.values_list("meta_lead_id", flat=True)),
                         ["1234567890123456", "6543210987654321"])
        # A later stage goes to both Meta leads.
        change_stage(lead, Stage.QUALIFICADO, actor="ana")
        self.assertEqual(MetaCrmEvent.objects.filter(event_name="Qualificado").count(), 2)


class PollTests(MetaTestBase):
    def test_reads_forms_and_skips_known_leads(self):
        fake = self.fake(leads=[graph_lead(), graph_lead(leadgen_id="6543210987654321", phone="+5551988887777", email="bia@exemplo.com")])
        self.assertEqual(meta.poll_page(self.page), 2)
        self.page.refresh_from_db()
        self.assertEqual(self.page.form_names, {"900": "Form Casa"})
        self.assertIsNotNone(self.page.polled_until)
        self.assertEqual(meta.poll_page(self.page), 0)
        self.assertEqual(Lead.objects.count(), 2)
        leads_call = [c for c in fake.calls if c[1] == "900/leads"][-1]
        since = json.loads(leads_call[2]["filtering"])[0]["value"]
        self.assertAlmostEqual(since, (self.page.polled_until - meta.POLL_OVERLAP).timestamp(), delta=1)

    def test_first_reading_goes_back_90_days(self):
        fake = self.fake()
        meta.poll_page(self.page)
        since = json.loads([c for c in fake.calls if c[1] == "900/leads"][0][2]["filtering"])[0]["value"]
        self.assertAlmostEqual(since, (timezone.now() - timedelta(days=90)).timestamp(), delta=5)


class FunnelTests(MetaTestBase):
    def setUp(self):
        super().setUp()
        self.fake_graph = self.fake(leads=[graph_lead()])
        meta.fetch_lead.call(self.page.pk, "1234567890123456")
        self.lead = Lead.objects.get()

    def test_stage_change_records_history_and_meta_event(self):
        self.assertTrue(change_stage(self.lead, Stage.EM_ANALISE, actor="ana", reason="ligou"))
        self.assertFalse(change_stage(self.lead, Stage.EM_ANALISE, actor="ana"))
        change_stage(self.lead, Stage.CONVERTIDO, actor="ana")
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage, Stage.CONVERTIDO)
        self.assertIsNotNone(self.lead.closed_at)
        history = [(d["from"], d["to"]) for d in LeadEvent.objects.filter(type=EventType.STAGE_CHANGED).values_list("data", flat=True)]
        self.assertEqual(history, [("criado", "em_analise"), ("em_analise", "convertido")])
        self.assertEqual(list(MetaCrmEvent.objects.values_list("event_name", flat=True)),
                         ["Criado", "Em análise", "Convertido"])

    def test_leads_from_other_sources_send_nothing(self):
        other = Lead.objects.create(tenant=self.tenant, contact=Contact.objects.create(tenant=self.tenant, email="b@x.com"),
                                    source="whatsapp")
        change_stage(other, Stage.QUALIFICADO, actor="ana")
        self.assertEqual(MetaCrmEvent.objects.filter(lead=other).count(), 0)

    def test_send_payload(self):
        change_stage(self.lead, Stage.QUALIFICADO, actor="ana")
        self.assertEqual(meta.flush_events(), (2, 0, 0))
        post = [c for c in self.fake_graph.calls if c[1] == "777/events"][0]
        events = json.loads(post[3]["data"])
        self.assertEqual([e["event_name"] for e in events], ["Criado", "Qualificado"])
        self.assertEqual(events[1]["action_source"], "system_generated")
        self.assertEqual(events[1]["user_data"], {"lead_id": 1234567890123456})
        self.assertEqual(events[1]["custom_data"], {"event_source": "crm", "lead_event_source": "Agitare CRM"})
        self.assertNotIn("test_event_code", post[3])
        self.assertEqual(MetaCrmEvent.objects.filter(status="sent").count(), 2)

    @override_settings(META_TEST_EVENT_CODE="TEST123")
    def test_test_event_code(self):
        meta.flush_events()
        post = [c for c in self.fake_graph.calls if c[1] == "777/events"][0]
        self.assertEqual(post[3]["test_event_code"], "TEST123")

    @override_settings(META_CAPI_ENABLED=False)
    def test_nothing_is_sent_when_disabled(self):
        self.assertEqual(meta.flush_events(), (0, 0, 0))
        self.assertFalse([c for c in self.fake_graph.calls if c[1].endswith("/events")])

    def test_events_older_than_7_days_expire(self):
        MetaCrmEvent.objects.update(event_time=timezone.now() - timedelta(days=8))
        self.assertEqual(meta.flush_events(), (0, 0, 1))
        self.assertEqual(MetaCrmEvent.objects.get().status, "expired")

    def test_temporary_error_retries_later(self):
        self.fake_graph.fail_events = lambda data: meta.MetaError("HTTP 500", status=500)
        self.assertEqual(meta.flush_events(), (0, 1, 0))
        event = MetaCrmEvent.objects.get()
        self.assertEqual((event.status, event.attempts), ("pending", 1))
        self.assertGreater(event.next_attempt_at, timezone.now())
        self.assertEqual(meta.flush_events(), (0, 0, 0))  # not due yet

    def test_bad_event_is_isolated_from_the_batch(self):
        change_stage(self.lead, Stage.EM_ANALISE, actor="ana")
        MetaCrmEvent.objects.filter(event_name="Criado").update(meta_lead_id="1")

        def fail(data):
            if any(e["user_data"]["lead_id"] == 1 for e in json.loads(data["data"])):
                return meta.MetaError("HTTP 400 lead_id inválido", status=400, permanent=True)
            return None

        def events(method, path, token=None, params=None, data=None):
            if path.endswith("/events"):
                error = fail(data)
                if error:
                    raise error
                return {"events_received": 1}
            return FakeGraph.__call__(self.fake_graph, method, path, token, params, data)

        with mock.patch.object(meta, "graph", events):
            self.assertEqual(meta.flush_events(), (1, 1, 0))
        self.assertEqual(MetaCrmEvent.objects.get(event_name="Criado").status, "failed")
        self.assertEqual(MetaCrmEvent.objects.get(event_name="Em análise").status, "sent")
