import hashlib
import hmac
import json
import time
import uuid
from unittest import mock

from django.test import TestCase, override_settings
from django_tasks_db.models import DBTaskResult

from .chatwoot import process_message
from .models import Contact, EventType, Lead, LeadEvent, Tenant

SECRET = "seg-conta-5"


def message(msg_id=1, conversation_id=10, mtype="incoming", channel="Channel::Whatsapp", private=False,
            referral=None, sender=None, source_id="5551999998888", extra=None):
    return {
        "event": "message_created",
        "id": msg_id,
        "account": {"id": 5, "name": "Cliente"},
        "message_type": mtype,
        "private": private,
        "content": "texto que nunca é guardado",
        "content_attributes": {"referral": referral} if referral else {},
        "conversation": {"id": conversation_id, "channel": channel, "contact_inbox": {"source_id": source_id},
                         "additional_attributes": extra or {}},
        "sender": sender if sender is not None else {"id": 77, "name": "Ana", "phone_number": "+5551999998888", "email": ""},
    }


@override_settings(CORE_API_URL="https://core.example", CORE_WEBHOOK_SECRET="core-seg")
class ChatwootWebhookTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(uuid=uuid.uuid4(), name="Cliente", slug="cliente", chatwoot_account_id=5)

    def post(self, payload, secret=SECRET, ts=None):
        body = json.dumps(payload).encode()
        ts = str(int(time.time()) if ts is None else ts)
        sig = "sha256=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        return self.client.post("/webhooks/chatwoot/", data=body, content_type="application/json",
                                HTTP_X_CHATWOOT_TIMESTAMP=ts, HTTP_X_CHATWOOT_SIGNATURE=sig)

    def core_answers(self, secret):
        return mock.patch("crm.chatwoot.requests.get", return_value=mock.Mock(
            status_code=200, json=lambda: {"account_id": 5, "webhook_secret": secret}))

    def test_first_call_reads_the_secret_from_the_core_then_queues(self):
        with self.core_answers(SECRET) as get:
            self.assertEqual(self.post(message()).status_code, 200)
        self.assertIn(f"/api/v1/tenants/{self.tenant.uuid}/chatwoot", get.call_args.args[0])
        self.assertEqual(Tenant.objects.get().chatwoot_webhook_secret, SECRET)
        self.assertEqual(DBTaskResult.objects.filter(task_path="crm.chatwoot.process_message").count(), 1)
        with mock.patch("crm.chatwoot.requests.get") as get:  # cached now
            self.post(message(msg_id=2))
        get.assert_not_called()

    def test_bad_old_or_unknown_calls_are_refused(self):
        with self.core_answers(SECRET):
            self.assertEqual(self.post(message(), secret="errado").status_code, 403)
            self.assertEqual(self.post(message(), ts=int(time.time()) - 3600).status_code, 403)
        other = message()
        other["account"]["id"] = 999
        self.assertEqual(self.post(other).status_code, 403)
        self.assertFalse(DBTaskResult.objects.exists())


class ProcessMessageTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(uuid=uuid.uuid4(), name="Cliente", slug="cliente", chatwoot_account_id=5)

    def process(self, payload):
        return process_message.call(self.tenant.pk, payload)

    def test_first_whatsapp_message_opens_a_lead(self):
        self.process(message())
        lead = Lead.objects.get()
        self.assertEqual((lead.source, lead.conversation_id, lead.external_id), ("whatsapp", 10, "chatwoot:5:10"))
        contact = lead.contact
        self.assertEqual((contact.phone_e164, contact.whatsapp_wa_id, contact.chatwoot_contact_id), ("+5551999998888", "5551999998888", 77))
        self.assertEqual([e.type for e in lead.events.all()], [EventType.CREATED, EventType.MESSAGE])
        msg_event = lead.events.get(type=EventType.MESSAGE)
        self.assertEqual(msg_event.data["message_id"], 1)
        self.assertNotIn("texto que nunca é guardado", json.dumps(msg_event.data))

    def test_click_to_whatsapp_ad_becomes_meta_attribution(self):
        referral = {"source_type": "ad", "source_id": "120000000003", "ctwa_clid": "ctwa-1", "headline": "Apê no centro",
                    "body": "2 quartos", "source_url": "https://fb.me/xyz", "image_url": "https://img.example/a.jpg"}
        self.process(message(referral=referral))
        att = Lead.objects.get().attributions.get()
        self.assertEqual((att.platform, att.kind, att.placement, att.ad_id, att.ctwa_clid), ("meta", "ctwa", "whatsapp", "120000000003", "ctwa-1"))
        self.assertEqual((att.creative_headline, att.creative_media_url), ("Apê no centro", "https://img.example/a.jpg"))
        self.assertFalse(att.is_organic)
        self.assertEqual(att.raw["referral"]["ctwa_clid"], "ctwa-1")

    def test_site_chat_takes_the_utms_of_the_page(self):
        self.process(message(channel="Channel::WebWidget", source_id="visitor-1", sender={"id": 9, "name": "Bia", "email": "bia@x.com"},
                         extra={"referer": "https://site.example/imovel?utm_source=google&utm_campaign=verao&gclid=g1"}))
        lead = Lead.objects.get()
        att = lead.attributions.get()
        self.assertEqual(lead.source, "site_chat")
        self.assertEqual((att.platform, att.kind, att.utm_campaign, att.gclid), ("google", "landing_page", "verao", "g1"))

    def test_instagram_contact_is_found_by_channel_id(self):
        self.process(message(channel="Channel::Instagram", source_id="ig-123", sender={"id": 3, "name": "Caio"}))
        self.assertEqual(Contact.objects.get().instagram_id, "ig-123")
        self.assertEqual(Lead.objects.get().source, "instagram")

    def test_following_messages_are_events_only_once_each(self):
        self.process(message(msg_id=1))
        self.process(message(msg_id=2, mtype="outgoing", sender={"id": 4, "name": "Atendente", "type": "user"}))
        self.process(message(msg_id=2, mtype="outgoing", sender={"id": 4, "name": "Atendente", "type": "user"}))  # repeated
        self.process(message(msg_id=3))
        lead = Lead.objects.get()
        self.assertEqual(Lead.objects.count(), 1)
        self.assertEqual(lead.events.filter(type=EventType.MESSAGE).count(), 3)
        out = lead.events.get(data__message_id=2)
        self.assertEqual((out.data["direction"], out.actor), ("outgoing", "Atendente"))

    def test_private_notes_activity_and_agent_first_are_ignored(self):
        self.process(message(mtype="outgoing"))  # agent wrote first: no lead yet
        self.process(message(msg_id=5, private=True))
        self.process(message(msg_id=6, mtype="activity"))
        self.assertFalse(Lead.objects.exists())

    def test_contact_already_with_an_open_lead_gets_the_conversation(self):
        from .ingest import ingest

        form = ingest(self.tenant, {"source": "meta_form", "phone": "51999998888"})
        self.process(message())
        lead = Lead.objects.get()
        self.assertEqual(lead, form.lead)
        self.assertEqual(lead.conversation_id, 10)
        self.assertEqual(lead.events.filter(type=EventType.REENTRY).count(), 1)
