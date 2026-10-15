import uuid
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from leads.models import UserProfile

from . import funnel, messaging
from .chatwoot import process_message
from .chatwoot_actions import ChatwootUnavailable
from .ingest import ingest
from .models import (
    Channel, Contact, EventType, InitialMessage, Lead, LeadEvent, OutboundMessage, Stage, Substage, Suppression, Tenant,
    TrackedLink,
)

EMAIL_INBOX = {"id": 3, "name": "E-mail", "channel_type": "Channel::Email", "email": "contato@cliente.com", "phone_number": ""}
WA_INBOX = {"id": 4, "name": "WhatsApp", "channel_type": "Channel::Whatsapp", "email": "", "phone_number": "+5551900000000"}


class FakeChatwoot:
    """Bot API (conversations, messages) and the core's contact endpoint."""

    def __init__(self):
        self.conversations, self.messages, self.can_reply, self.contacts = {}, [], {}, []
        self.fail = None

    def bot(self, tenant, method, path, payload=None):
        if self.fail:
            raise self.fail
        if method == "POST" and path == "conversations":
            cid = 100 + len(self.conversations)
            self.conversations[cid] = payload
            self.messages.append((cid, payload["message"]))
            return {"id": cid, "messages": [{"id": 9000 + len(self.messages)}]}
        parts = path.split("/")
        cid = int(parts[1])
        if method == "GET" and len(parts) == 2:
            return {"id": cid, "can_reply": self.can_reply.get(cid, True), "status": "open"}
        if method == "POST" and parts[2] == "messages":
            self.messages.append((cid, payload))
            return {"id": 9000 + len(self.messages), "content": payload["content"]}
        raise AssertionError(f"inesperado {method} {path}")

    def core_post(self, url, json=None, headers=None, timeout=None):
        self.contacts.append(json)
        return mock.Mock(status_code=200, json=lambda: {"contact_id": 77})


@override_settings(CHATWOOT_URL="https://chat.example", CORE_API_URL="https://core.example", CORE_WEBHOOK_SECRET="seg",
                   SITE_URL="https://leads.example")
class MessagingTestBase(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(uuid=uuid.uuid4(), name="Imóveis Alto", slug="alto", chatwoot_account_id=5,
                                            chatwoot_bot_token="tok", chatwoot_inboxes=[EMAIL_INBOX, WA_INBOX])
        self.cw = FakeChatwoot()
        for p in (mock.patch("crm.messaging.bot_api", side_effect=self.cw.bot),
                  mock.patch("crm.messaging.requests.post", side_effect=self.cw.core_post)):
            p.start()
            self.addCleanup(p.stop)

    def form_lead(self, source="agipages_form", consent=True, email="ana@x.com", phone="51999998888", **extra):
        data = {"source": source, "name": "Ana Souza", "email": email, "phone": phone,
                "external_id": str(uuid.uuid4()),
                "landing_page": {"id": str(uuid.uuid4()), "name": "Residencial Mar"} if source == "agipages_form" else {},
                "attribution": {"form_id": "f1", "form_name": "Form Praia"} if source != "agipages_form" else {}, **extra}
        if consent is not None:
            data["consent"] = {"email": consent, "whatsapp": consent}
        return ingest(self.tenant, data).lead

    def plan(self, lead):
        messaging.plan_initial.call(lead.pk)
        return {m.channel: m for m in OutboundMessage.objects.filter(lead=lead)}


class InitialMessageTests(MessagingTestBase):
    def test_email_initial_message_end_to_end(self):
        InitialMessage.objects.create(tenant=self.tenant, source="", form_key="", email_subject="Oi, {primeiro_nome}",
                                      email_body="Recebemos seu contato pela {pagina}.\n\nVeja https://alto.com/imovel?x=1",
                                      send_whatsapp=False)
        lead = self.form_lead()
        planned = self.plan(lead)
        email = planned[Channel.EMAIL]
        self.assertEqual((email.status, email.subject, email.inbox_id), ("queued", "Oi, Ana", 3))
        self.assertEqual(messaging.process_due(), 1)
        email.refresh_from_db()
        self.assertEqual(email.status, "sent")
        cid, message = self.cw.messages[0]
        conversation = self.cw.conversations[cid]
        self.assertEqual((conversation["inbox_id"], conversation["contact_id"]), (3, 77))
        self.assertEqual(conversation["additional_attributes"], {"mail_subject": "Oi, Ana"})
        self.assertEqual(self.cw.contacts[0], {"name": "Ana Souza", "email": "ana@x.com", "phone": "+5551999998888"})
        html = message["email_html_content"]
        self.assertIn("Recebemos seu contato pela Residencial Mar.", html)
        link = TrackedLink.objects.get()
        self.assertEqual(link.url, "https://alto.com/imovel?x=1")
        self.assertIn(f"https://leads.example/t/{link.token}/", html)
        self.assertIn(f"https://leads.example/descadastro/{email.uuid}/", html)
        self.assertIn(f"https://leads.example/o/{email.uuid}.gif", html)
        self.assertIn("descadastro", message["content"])
        lead.refresh_from_db()
        self.assertEqual(lead.conversation_id, cid)
        self.assertTrue(LeadEvent.objects.filter(lead=lead, type=EventType.MESSAGE_SENT).exists())

    def test_consent(self):
        agipages_without = self.plan(self.form_lead(consent=None))
        self.assertEqual(agipages_without[Channel.EMAIL].reason, "sem consentimento para esse canal")
        refused = self.plan(self.form_lead(consent=False, email="b@x.com", phone="51988887777"))
        self.assertEqual(refused[Channel.EMAIL].status, "skipped")
        meta = self.plan(self.form_lead(source="meta_form", consent=None, email="c@x.com", phone="51977776666"))
        self.assertEqual(meta[Channel.EMAIL].status, "queued")  # the platform form's privacy policy

    def test_whatsapp_needs_a_template_and_an_inbox(self):
        lead = self.form_lead()
        self.assertEqual(self.plan(lead)[Channel.WHATSAPP].reason, "sem template aprovado do WhatsApp para iniciar a conversa")
        default = messaging.default_initial_message(self.tenant)
        default.whatsapp_template, default.send_email = "boas_vindas", False
        default.save()
        lead2 = self.form_lead(email="d@x.com", phone="51966665555")
        wa = self.plan(lead2)[Channel.WHATSAPP]
        self.assertEqual((wa.status, wa.template["params"]), ("queued", ["Ana", "Residencial Mar"]))
        messaging.process_due()
        payload = self.cw.messages[-1][1]
        self.assertEqual(payload["template_params"], {"name": "boas_vindas", "language": "pt_BR",
                                                      "processed_params": {"1": "Ana", "2": "Residencial Mar"}})
        Tenant.objects.filter(pk=self.tenant.pk).update(chatwoot_inboxes=[EMAIL_INBOX])
        self.tenant.refresh_from_db()
        lead3 = Lead.objects.get(pk=self.form_lead(email="e@x.com", phone="51955554444").pk)
        self.assertIn("não tem inbox de WhatsApp", self.plan(lead3)[Channel.WHATSAPP].reason)

    def test_old_leads_get_no_automatic_initial_message(self):
        lead = self.form_lead(occurred_at=(timezone.now() - timedelta(days=3)).isoformat())
        messaging.plan_initial.call(lead.pk)
        msg = OutboundMessage.objects.get(lead=lead)
        self.assertEqual(msg.status, "skipped")
        self.assertIn("lead antigo", msg.reason)
        messaging.plan_initial.call(lead.pk, "Dona Ana")  # a person, on purpose
        self.assertTrue(OutboundMessage.objects.filter(lead=lead, status="queued").exists())

    def test_form_with_its_own_message(self):
        lead = self.form_lead(source="meta_form", consent=None)
        InitialMessage.objects.create(tenant=self.tenant, source="meta_form", form_key="f1", email_subject="Form próprio",
                                      email_body="Oi", send_whatsapp=False)
        self.assertEqual(self.plan(lead)[Channel.EMAIL].subject, "Form próprio")

    def test_resend_window_and_resend_on_purpose(self):
        lead = self.form_lead()
        self.plan(lead)
        messaging.process_due()
        Lead.objects.filter(pk=lead.pk).update(stage=Stage.PERDIDO)
        again = self.form_lead()  # same contact, new lead
        second = self.plan(again)[Channel.EMAIL]
        self.assertIn("já enviada a este contato", second.reason)
        forced = messaging.resend(second, actor="Dona Ana", reason="pediu de novo")
        self.assertEqual((forced.status, forced.forced_by), ("queued", "Dona Ana"))
        self.assertTrue(LeadEvent.objects.filter(lead=again, reason="Reenvio de propósito: pediu de novo").exists())

    def test_suppression_and_unsubscribe(self):
        lead = self.form_lead()
        self.plan(lead)
        messaging.process_due()
        email = OutboundMessage.objects.get(lead=lead, channel=Channel.EMAIL)
        self.assertContains(self.client.get(f"/descadastro/{email.uuid}/"), "Confirmar descadastro")
        self.assertContains(self.client.post(f"/descadastro/{email.uuid}/"), "não vai mais receber")
        self.assertTrue(Suppression.objects.filter(tenant=self.tenant, address="ana@x.com", reason="unsubscribe").exists())
        self.assertFalse(Contact.objects.get(pk=lead.contact_id).consent_email)
        self.assertTrue(LeadEvent.objects.filter(lead=lead, type=EventType.UNSUBSCRIBED).exists())
        Suppression.objects.create(tenant=self.tenant, address="z@x.com", reason="manual")
        blocked = self.plan(self.form_lead(email="z@x.com", phone="51911112222"))
        self.assertEqual(blocked[Channel.EMAIL].reason, "endereço na lista de bloqueio")

    def test_click_and_open_tracking(self):
        InitialMessage.objects.create(tenant=self.tenant, source="", form_key="", email_subject="Oi",
                                      email_body="Veja https://alto.com/x", send_whatsapp=False)
        lead = self.form_lead()
        self.plan(lead)
        messaging.process_due()
        email = OutboundMessage.objects.get(lead=lead, channel=Channel.EMAIL)
        link = TrackedLink.objects.get()
        r = self.client.get(f"/t/{link.token}/")
        self.assertEqual((r.status_code, r["Location"]), (302, "https://alto.com/x"))
        link.refresh_from_db()
        self.assertEqual(link.clicks, 1)
        for _ in range(2):
            r = self.client.get(f"/o/{email.uuid}.gif")
        self.assertEqual(r["Content-Type"], "image/gif")
        self.assertEqual(LeadEvent.objects.filter(lead=lead, type=EventType.EMAIL_OPENED).count(), 1)
        self.assertEqual(LeadEvent.objects.filter(lead=lead, type=EventType.EMAIL_CLICKED).count(), 1)
        self.assertEqual(self.client.get("/t/nao-existe/").status_code, 404)

    def test_failures_retry_then_give_up(self):
        lead = self.form_lead()
        self.plan(lead)
        self.cw.fail = ChatwootUnavailable("fora do ar")
        email = OutboundMessage.objects.get(lead=lead, channel=Channel.EMAIL)
        now = timezone.now()
        for i in range(messaging.MAX_ATTEMPTS):
            messaging.process_due(now + timedelta(days=i + 1))
        email.refresh_from_db()
        self.assertEqual((email.status, email.attempts), ("failed", messaging.MAX_ATTEMPTS))


class EmailLimitsTests(MessagingTestBase):
    def sent(self, n, when):
        lead = self.form_lead()
        for _ in range(n):
            OutboundMessage.objects.create(tenant=self.tenant, lead=lead, kind="initial", channel=Channel.EMAIL, inbox_id=3,
                                           status="sent", sent_at=when, send_after=when)

    def test_hourly_cap_spacing_and_warmup(self):
        now = timezone.now()
        self.assertIsNone(messaging.email_slot(self.tenant, 3, now))
        self.sent(1, now - timedelta(seconds=30))
        self.assertIsNotNone(messaging.email_slot(self.tenant, 3, now))  # 30/h → one every 2 minutes
        OutboundMessage.objects.all().delete()
        self.sent(20, now - timedelta(minutes=70))  # first week: 20 a day
        later = messaging.email_slot(self.tenant, 3, now)
        if timezone.localtime(now - timedelta(minutes=70)).date() == timezone.localtime(now).date():
            self.assertGreater(later, now + timedelta(hours=1))
        OutboundMessage.objects.all().delete()
        Tenant.objects.filter(pk=self.tenant.pk).update(email_hourly_cap=5)
        self.tenant.refresh_from_db()
        self.sent(5, now - timedelta(minutes=30))
        self.assertGreater(messaging.email_slot(self.tenant, 3, now), now)

    def test_over_the_limit_waits_in_the_queue(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(email_hourly_cap=1)
        self.tenant.refresh_from_db()
        first, second = self.form_lead(), self.form_lead(email="b@x.com", phone="51988887777")
        self.plan(first)
        self.plan(second)
        messaging.process_due()
        statuses = sorted(OutboundMessage.objects.filter(channel=Channel.EMAIL).values_list("status", flat=True))
        self.assertEqual(statuses, ["queued", "sent"])
        waiting = OutboundMessage.objects.get(channel=Channel.EMAIL, status="queued")
        self.assertGreater(waiting.send_after, timezone.now() + timedelta(minutes=50))


class SubstageMessageTests(MessagingTestBase):
    def setUp(self):
        super().setUp()
        self.visita = Substage.objects.create(tenant=self.tenant, stage=Stage.EM_ANALISE, name="Visita",
                                              message_text="Oi {primeiro_nome}, sua visita está confirmada.")

    def test_message_in_the_conversation_when_entering(self):
        lead = ingest(self.tenant, {"source": "whatsapp", "name": "Bia", "phone": "51988887777", "conversation_id": 42}).lead
        with self.captureOnCommitCallbacks(execute=True):
            funnel.move(lead, substage=self.visita, actor="ana")
        messaging.plan_substage.call(lead.pk, self.visita.pk)
        msg = OutboundMessage.objects.get(lead=lead)
        self.assertEqual((msg.kind, msg.channel, msg.conversation_id, msg.priority), ("substage", "whatsapp", 42, 1))
        messaging.process_due()
        self.assertEqual(self.cw.messages[-1], (42, {"content": "Oi Bia, sua visita está confirmada.", "message_type": "outgoing"}))
        # Entering again within the window does not send it again.
        messaging.plan_substage.call(lead.pk, self.visita.pk)
        self.assertIn("já enviada", OutboundMessage.objects.filter(lead=lead).first().reason)

    def test_outside_the_24h_window_whatsapp_does_not_send_free_text(self):
        lead = ingest(self.tenant, {"source": "whatsapp", "name": "Bia", "phone": "51988887777", "conversation_id": 43}).lead
        self.cw.can_reply[43] = False
        messaging.plan_substage.call(lead.pk, self.visita.pk)
        messaging.process_due()
        msg = OutboundMessage.objects.get(lead=lead)
        self.assertEqual((msg.status, msg.reason), ("skipped", "fora da janela de 24 h do canal (só template ou atendente)"))

    def test_sub_stage_without_message_sends_nothing(self):
        quiet = Substage.objects.create(tenant=self.tenant, stage=Stage.QUALIFICADO, name="Quente")
        lead = ingest(self.tenant, {"source": "whatsapp", "phone": "51988887777", "conversation_id": 44}).lead
        with self.captureOnCommitCallbacks(execute=True):
            funnel.move(lead, substage=quiet, actor="ana")
        self.assertFalse(OutboundMessage.objects.exists())


class BounceTests(MessagingTestBase):
    def test_delivery_failure_suppresses_the_address_and_makes_no_lead(self):
        lead = self.form_lead()
        self.plan(lead)
        messaging.process_due()
        leads_before = Lead.objects.count()
        payload = {"event": "message_created", "id": 1, "account": {"id": 5}, "message_type": "incoming",
                   "content": "Delivery failed for <ana@x.com>: user unknown",
                   "conversation": {"id": 500, "channel": "Channel::Email"},
                   "sender": {"id": 1, "name": "Mail Delivery System", "email": "MAILER-DAEMON@mail.example"}}
        self.assertIn("ana@x.com", process_message.call(self.tenant.pk, payload))
        self.assertEqual(Lead.objects.count(), leads_before)
        self.assertTrue(Suppression.objects.filter(address="ana@x.com", reason="bounce").exists())


@override_settings(AUTHENTICATION_BACKENDS=["django.contrib.auth.backends.ModelBackend"], CORE_API_URL="")
class MessagesScreenTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(uuid=uuid.uuid4(), name="Alto", slug="alto", chatwoot_inboxes=[EMAIL_INBOX])
        user = User.objects.create_user(username="dona")
        UserProfile.objects.create(user=user, core_client_uuid=self.tenant.uuid)
        self.client.force_login(user)
        ingest(self.tenant, {"source": "meta_form", "email": "a@x.com", "external_id": "1",
                             "attribution": {"form_id": "f9", "form_name": "Form Verão"}})

    def test_page_forms_override_and_back_to_default(self):
        page = self.client.get("/crm/mensagens/")
        self.assertContains(page, "Mensagem inicial padrão")
        self.assertContains(page, "Form Verão")
        self.assertContains(page, "usando o padrão")
        self.client.post("/crm/mensagens/inicial/", {"source": "meta_form", "form_key": "f9", "form_label": "Form Verão",
                                                     "is_active": "on", "send_email": "on", "email_subject": "Verão",
                                                     "email_body": "Oi"})
        self.assertContains(self.client.get("/crm/mensagens/"), "mensagem própria")
        self.client.post("/crm/mensagens/inicial/", {"source": "meta_form", "form_key": "f9", "use_default": "on"})
        self.assertFalse(InitialMessage.objects.filter(form_key="f9").exists())

    def test_limits_and_suppression_list(self):
        self.client.post("/crm/mensagens/limites/", {"resend_window_days": "3", "email_hourly_cap": "10", "email_daily_cap": "80"})
        self.tenant.refresh_from_db()
        self.assertEqual((self.tenant.resend_window_days, self.tenant.email_hourly_cap, self.tenant.email_daily_cap), (3, 10, 80))
        self.client.post("/crm/mensagens/bloqueio/", {"address": "Spam@X.com"})
        s = Suppression.objects.get()
        self.assertEqual((s.address, s.reason), ("spam@x.com", "manual"))
        self.client.post(f"/crm/mensagens/bloqueio/{s.pk}/remover/")
        self.assertFalse(Suppression.objects.exists())
