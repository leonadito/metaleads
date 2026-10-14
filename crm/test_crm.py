import hashlib
import hmac
import json
import time
import uuid
from unittest import mock

from django.test import TestCase, override_settings

from .ingest import IngestError, ingest, normalize_phone
from .models import ApiToken, Contact, EventType, Lead, LeadAttribution, LeadEvent, Stage, Tenant


def make_tenant(slug="pousada", status="active"):
    return Tenant.objects.create(uuid=uuid.uuid4(), name=slug.title(), slug=slug, status=status)


class NormalizePhoneTests(TestCase):
    def test_brazilian_formats_become_e164(self):
        for raw in ["(51) 99999-8888", "51999998888", "+55 51 99999-8888", "055 51 999998888"]:
            self.assertEqual(normalize_phone(raw), "+5551999998888", raw)

    def test_invalid_is_empty(self):
        self.assertEqual(normalize_phone("123"), "")
        self.assertEqual(normalize_phone(""), "")


class IngestTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant()

    def test_new_entry_opens_a_lead_with_attribution_and_history(self):
        r = ingest(self.tenant, {
            "source": "meta_form", "external_id": "lg-1", "name": "Ana", "phone": "(51) 99999-8888",
            "email": "Ana@X.com", "form_answers": {"quartos": "3"},
            "attribution": {"platform": "meta", "kind": "form", "campaign_id": "c1", "form_id": "f1",
                            "placement": "instagram", "raw": {"leadgen_id": "lg-1"}},
        })
        self.assertTrue(r.created)
        self.assertEqual(r.contact.phone_e164, "+5551999998888")
        self.assertEqual(r.contact.email, "ana@x.com")
        self.assertEqual(r.lead.stage, Stage.CRIADO)
        att = r.lead.attributions.get()
        self.assertEqual((att.platform, att.kind, att.campaign_id, att.placement), ("meta", "form", "c1", "instagram"))
        self.assertEqual(att.raw, {"leadgen_id": "lg-1"})
        self.assertEqual(r.lead.events.get().type, EventType.CREATED)

    def test_same_person_by_phone_or_email_is_one_contact_and_one_open_lead(self):
        first = ingest(self.tenant, {"source": "agipages_form", "phone": "51999998888", "attribution": {"utm_campaign": "a"}})
        again = ingest(self.tenant, {"source": "meta_form", "email": "ana@x.com", "phone": "+55 51 99999-8888",
                                     "form_answers": {"cidade": "Torres"}, "attribution": {"campaign_id": "b"}})
        self.assertFalse(again.created)
        self.assertEqual(again.lead, first.lead)
        self.assertEqual(Contact.objects.count(), 1)
        self.assertEqual(Contact.objects.get().email, "ana@x.com")  # blank filled
        lead = Lead.objects.get()
        self.assertEqual(lead.form_answers, {"cidade": "Torres"})
        self.assertEqual(lead.attributions.count(), 2)  # first and latest campaign kept
        self.assertEqual([e.type for e in lead.events.all()], [EventType.CREATED, EventType.REENTRY])

    def test_after_the_lead_closes_a_new_entry_opens_another(self):
        first = ingest(self.tenant, {"source": "manual", "phone": "51999998888"})
        Lead.objects.filter(pk=first.lead.pk).update(stage=Stage.PERDIDO)
        second = ingest(self.tenant, {"source": "manual", "phone": "51999998888"})
        self.assertTrue(second.created)
        self.assertEqual(Lead.objects.count(), 2)

    def test_retried_source_event_never_duplicates(self):
        data = {"source": "meta_form", "external_id": "lg-9", "phone": "51999998888"}
        first = ingest(self.tenant, data)
        retry = ingest(self.tenant, data)
        self.assertTrue(retry.duplicate)
        self.assertEqual(retry.lead, first.lead)
        self.assertEqual(LeadEvent.objects.count(), 1)
        # A retried *re-entry* is also recognised (its id lives in the history).
        ingest(self.tenant, {"source": "whatsapp", "external_id": "wamid-1", "phone": "51999998888"})
        again = ingest(self.tenant, {"source": "whatsapp", "external_id": "wamid-1", "phone": "51999998888"})
        self.assertTrue(again.duplicate)
        self.assertEqual(LeadEvent.objects.count(), 2)

    def test_tenants_never_share_contacts(self):
        other = make_tenant("outra")
        ingest(self.tenant, {"source": "manual", "phone": "51999998888"})
        ingest(other, {"source": "manual", "phone": "51999998888"})
        self.assertEqual(Contact.objects.count(), 2)
        self.assertEqual(Lead.objects.filter(tenant=other).count(), 1)

    def test_known_data_is_not_overwritten(self):
        ingest(self.tenant, {"source": "manual", "phone": "51999998888", "name": "Ana Lima"})
        ingest(self.tenant, {"source": "manual", "phone": "51999998888", "name": "ana"})
        self.assertEqual(Contact.objects.get().name, "Ana Lima")

    def test_channel_only_contact_and_consent(self):
        r = ingest(self.tenant, {"source": "instagram", "instagram_id": "ig-77",
                                 "consent": {"whatsapp": True, "email": False, "source": "form f1"}})
        self.assertEqual(r.contact.instagram_id, "ig-77")
        self.assertTrue(r.contact.consent_whatsapp)
        self.assertFalse(r.contact.consent_email)
        self.assertEqual(r.contact.consent_source, "form f1")

    def test_refusals(self):
        with self.assertRaises(IngestError):
            ingest(self.tenant, {"source": "manual", "name": "sem contato"})
        with self.assertRaises(IngestError):
            ingest(self.tenant, {"source": "pombo-correio", "phone": "51999998888"})
        with self.assertRaises(IngestError):
            ingest(make_tenant("inativa", status="inactive"), {"source": "manual", "phone": "51999998888"})

    def test_invalid_phone_is_reported_and_email_still_used(self):
        r = ingest(self.tenant, {"source": "manual", "phone": "123", "email": "a@x.com"})
        self.assertEqual(r.contact.phone_e164, "")
        self.assertIn("telefone inválido, guardado só nas respostas", r.warnings)


class ApiTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant()
        self.token, self.raw = ApiToken.issue(self.tenant, "agipages")

    def post(self, body, key="k1", token=None):
        return self.client.post(
            "/api/v1/leads", data=json.dumps(body), content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token or self.raw}", HTTP_IDEMPOTENCY_KEY=key,
        )

    def test_creates_then_replays_the_same_answer(self):
        body = {"source": "agipages_form", "phone": "51999998888", "landing_page": {"id": str(uuid.uuid4()), "name": "Alto do Parque", "url": "https://x.example/p/"}}
        first = self.post(body)
        self.assertEqual(first.status_code, 201)
        replay = self.post(body)
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(Lead.objects.count(), 1)
        self.assertEqual(Lead.objects.get().landing_page_name, "Alto do Parque")

    def test_same_key_with_another_body_is_409(self):
        self.post({"source": "manual", "phone": "51999998888"})
        self.assertEqual(self.post({"source": "manual", "phone": "51988887777"}).status_code, 409)

    def test_auth_key_and_body_checks(self):
        self.assertEqual(self.post({"phone": "1"}, token="errado").status_code, 401)
        self.token.revoked_at = self.token.created_at
        self.token.save()
        self.assertEqual(self.post({"phone": "51999998888"}).status_code, 401)
        _, raw = ApiToken.issue(self.tenant, "outro")
        self.assertEqual(self.post({"phone": "51999998888"}, key="", token=raw).status_code, 400)
        self.assertEqual(self.post({"name": "sem contato"}, key="k9", token=raw).status_code, 422)

    def test_token_only_writes_to_its_own_tenant(self):
        other = make_tenant("outra")
        self.post({"source": "manual", "phone": "51999998888", "tenant": str(other.uuid)})
        self.assertEqual(Lead.objects.get().tenant, self.tenant)


def signed(secret, body, ts=None):
    ts = int(time.time()) if ts is None else ts
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


@override_settings(CORE_WEBHOOK_SECRET="s3cret", CORE_API_URL="https://core.example")
class CoreSyncTests(TestCase):
    def event(self, etype, tenant, eid=None, updated_at="2026-10-14T10:00:00+00:00"):
        payload = {"id": tenant, "name": "Pousada", "slug": "pousada", "whitelabel_id": str(uuid.uuid4()),
                   "status": "inactive" if etype == "tenant.deactivated" else "active", "is_courtesy": False,
                   "custom_domain": "", "chatwoot_account_id": 7, "updated_at": updated_at}
        return {"id": eid or str(uuid.uuid4()), "type": etype, "created_at": updated_at, "data": {"tenant": payload}}

    def send(self, envelope, secret="s3cret", ts=None):
        body = json.dumps(envelope).encode()
        return self.client.post("/webhooks/core/", data=body, content_type="application/json",
                                HTTP_X_AGITARE_SIGNATURE=signed(secret, body, ts))

    def test_created_then_deactivated_updates_the_copy(self):
        tid = str(uuid.uuid4())
        self.assertEqual(self.send(self.event("tenant.created", tid)).status_code, 200)
        self.assertEqual(Tenant.objects.get(uuid=tid).chatwoot_account_id, 7)
        self.send(self.event("tenant.deactivated", tid, updated_at="2026-10-14T11:00:00+00:00"))
        self.assertEqual(Tenant.objects.get(uuid=tid).status, "inactive")

    def test_bad_or_old_signature_is_refused(self):
        tid = str(uuid.uuid4())
        self.assertEqual(self.send(self.event("tenant.created", tid), secret="outro").status_code, 403)
        self.assertEqual(self.send(self.event("tenant.created", tid), ts=int(time.time()) - 3600).status_code, 403)
        self.assertFalse(Tenant.objects.exists())

    def test_repeated_event_and_late_older_event_change_nothing(self):
        tid, eid = str(uuid.uuid4()), str(uuid.uuid4())
        self.send(self.event("tenant.deactivated", tid, eid=eid, updated_at="2026-10-14T11:00:00+00:00"))
        self.send(self.event("tenant.deactivated", tid, eid=eid))
        self.send(self.event("tenant.updated", tid, updated_at="2026-10-14T09:00:00+00:00"))  # older, late
        self.assertEqual(Tenant.objects.get(uuid=tid).status, "inactive")

    def test_full_sync_pulls_every_tenant(self):
        from .core_sync import full_sync

        tenants = [self.event("tenant.updated", str(uuid.uuid4()))["data"]["tenant"] for _ in range(3)]
        response = mock.Mock(status_code=200, json=lambda: {"tenants": tenants})
        response.raise_for_status = lambda: None
        with mock.patch("crm.core_sync.requests.get", return_value=response) as get:
            self.assertEqual(full_sync(), 3)
        self.assertEqual(get.call_args.kwargs["headers"], {"Authorization": "Bearer s3cret"})
        self.assertEqual(Tenant.objects.count(), 3)


class ServiceTokenTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant()
        self.other = make_tenant("outra")
        _, self.raw = ApiToken.issue(None, "agipages", service=True)

    def post(self, body, key):
        return self.client.post("/api/v1/leads", data=json.dumps(body), content_type="application/json",
                                HTTP_AUTHORIZATION=f"Bearer {self.raw}", HTTP_IDEMPOTENCY_KEY=key)

    def test_service_token_writes_to_the_tenant_named_in_the_body(self):
        self.assertEqual(self.post({"tenant": str(self.other.uuid), "source": "agipages_form", "phone": "51999998888"}, "a").status_code, 201)
        self.assertEqual(Lead.objects.get().tenant, self.other)

    def test_service_token_needs_a_known_tenant(self):
        self.assertEqual(self.post({"source": "agipages_form", "phone": "51999998888"}, "b").status_code, 422)
        self.assertEqual(self.post({"tenant": str(uuid.uuid4()), "phone": "51999998888"}, "c").status_code, 422)
        self.assertEqual(self.post({"tenant": "nao-e-uuid", "phone": "51999998888"}, "d").status_code, 422)
        inactive = make_tenant("inativa", status="inactive")
        self.assertEqual(self.post({"tenant": str(inactive.uuid), "phone": "51999998888"}, "e").status_code, 403)


class DailySyncTaskTests(TestCase):
    def test_ensure_enqueues_once(self):
        from django_tasks_db.models import DBTaskResult

        from .tasks import DAILY_SYNC_PATH, ensure_daily_sync

        self.assertTrue(ensure_daily_sync())
        self.assertFalse(ensure_daily_sync())
        self.assertEqual(DBTaskResult.objects.filter(task_path=DAILY_SYNC_PATH).count(), 1)

    def test_run_schedules_the_next_one_in_24h_or_1h_on_failure(self):
        from datetime import timedelta

        from django.utils import timezone
        from django_tasks_db.models import DBTaskResult

        from .tasks import DAILY_SYNC_PATH, sync_tenants_daily

        with mock.patch("crm.tasks.full_sync", return_value=3):
            self.assertEqual(sync_tenants_daily.call(), 3)
        nxt = DBTaskResult.objects.get(task_path=DAILY_SYNC_PATH)
        self.assertGreater(nxt.run_after, timezone.now() + timedelta(hours=23))
        nxt.delete()
        with mock.patch("crm.tasks.full_sync", side_effect=RuntimeError("core fora")):
            with self.assertRaises(RuntimeError):
                sync_tenants_daily.call()
        nxt = DBTaskResult.objects.get(task_path=DAILY_SYNC_PATH)
        self.assertLess(nxt.run_after, timezone.now() + timedelta(hours=2))
