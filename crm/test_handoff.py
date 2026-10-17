import json
import uuid
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from . import chatwoot_actions, funnel, handoff, llm, messaging, routing, sdr
from .ingest import ingest
from .models import EventType, Lead, LeadEvent, OutboundMessage, Stage, Substage, Tenant

MESSAGES = [{"id": 1, "direction": "incoming", "content": "Quero o de 3 quartos", "attachments": 0},
            {"id": 2, "direction": "outgoing", "content": "Fechado! Contrato assinado amanhã.", "attachments": 0}]


def tasks(path):
    return list(DBTaskResult.objects.filter(task_path=path).values_list("args_kwargs", flat=True))


def decision(**kw):
    data = {"acao": "manter", "etapa": "em_analise", "subetapa": "", "nova_subetapa_prompt": "", "motivo": "ok",
            "confianca": 0.9, **kw}
    return {"content": [{"type": "text", "text": json.dumps(data)}], "usage": {"total_tokens": 100}, "model": "m"}


@override_settings(CORE_API_URL="https://core.example", CORE_WEBHOOK_SECRET="seg", CHATWOOT_URL="https://cw.example")
class HandoffTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(uuid=uuid.uuid4(), name="Alto", slug="alto", chatwoot_account_id=5,
                                            chatwoot_bot_token="bot", ai_enabled_at=timezone.now())
        self.lead = ingest(self.tenant, {"source": "whatsapp", "name": "Bia", "phone": "51988887777",
                                         "conversation_id": 42}).lead

    def reload(self):
        self.lead.refresh_from_db()
        return self.lead

    def resolve(self):
        with self.captureOnCommitCallbacks(execute=True):
            return chatwoot_actions.conversation_changed.call(
                self.tenant.pk, {"event": "conversation_status_changed", "id": 42, "status": "resolved"})

    def test_resolution_lets_the_ai_read_and_move_without_answering(self):
        Lead.objects.filter(pk=self.lead.pk).update(frozen_at=timezone.now(), review_reason="vaivém")
        self.assertEqual(self.resolve(), "resolvida: a IA lê a conversa")
        self.assertEqual(tasks("crm.routing.route_lead"), [{"args": [self.lead.pk, True], "kwargs": {}}])
        self.assertIsNotNone(self.reload().resolved_at)
        with mock.patch.object(routing, "fetch_messages", return_value=MESSAGES), \
                mock.patch.object(chatwoot_actions, "bot_api", return_value={}), \
                mock.patch.object(llm, "call", side_effect=[decision()]) as call:
            routing.route_lead.call(self.lead.pk, True)
        self.assertIsNone(self.reload().frozen_at)  # out of review
        self.assertEqual(LeadEvent.objects.filter(type=EventType.RELEASED).get().actor, "IA")
        self.assertIn("A equipe acabou de resolver", call.call_args.args[2]["messages"][0]["content"][2]["text"])
        self.assertTrue(LeadEvent.objects.get(type=EventType.AI_DECISION).data["resolved"])
        self.assertEqual(tasks("crm.sdr.reply_lead"), [])  # closed: no reply

    def test_within_one_hour_the_people_take_it_after_that_the_ai(self):
        self.resolve()
        now = timezone.now()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertTrue(handoff.on_lead_message(self.reload(), now + timedelta(minutes=20)))
        lead = self.reload()
        self.assertEqual(lead.human_window_until, now + timedelta(minutes=80))
        self.assertIsNone(lead.resolved_at)
        self.assertIn({"args": [lead.pk, "open"], "kwargs": {}}, tasks("crm.handoff.set_status"))
        self.assertFalse(sdr.can_reply(lead))  # the people hold it
        # only the first message after the resolution counts
        self.assertFalse(handoff.on_lead_message(lead, now + timedelta(minutes=50)))
        self.assertEqual(self.reload().human_window_until, now + timedelta(minutes=80))
        # resolved more than an hour ago: the AI answers directly
        self.resolve()
        self.assertFalse(handoff.on_lead_message(self.reload(), timezone.now() + timedelta(minutes=61)))
        self.assertIsNone(self.reload().human_window_until)

    def test_a_persons_answer_keeps_it_otherwise_back_to_the_ai(self):
        Lead.objects.filter(pk=self.lead.pk).update(human_window_until=timezone.now() + timedelta(minutes=30))
        chatwoot_actions.message_seen(self.reload(), "outgoing", "user")
        self.assertIsNone(self.reload().human_window_until)
        chatwoot_actions.message_seen(self.reload(), "outgoing", "agent_bot")  # the bot does not count
        Lead.objects.filter(pk=self.lead.pk).update(human_window_until=timezone.now() - timedelta(minutes=1))
        self.assertEqual(handoff.return_expired(), 1)
        self.assertIn({"args": [self.lead.pk, "pending"], "kwargs": {}}, tasks("crm.handoff.set_status"))
        self.assertEqual(tasks("crm.sdr.reply_lead"), [{"args": [self.lead.pk], "kwargs": {}}])
        self.assertIsNone(self.reload().human_window_until)

    def test_review_sends_the_waiting_message_once(self):
        Lead.objects.filter(pk=self.lead.pk).update(waiting_since=timezone.now())
        with mock.patch.object(chatwoot_actions, "bot_api", return_value={"payload": []}):
            with self.captureOnCommitCallbacks(execute=True):
                funnel.send_to_review(self.reload(), "confiança baixa", "IA", {})
            chatwoot_actions.sync_lead.call(self.lead.pk, True, False)
        self.assertTrue(tasks("crm.messaging.plan_wait"))
        self.assertEqual(messaging.plan_wait.call(self.lead.pk), "whatsapp:queued")
        msg = OutboundMessage.objects.get(kind=OutboundMessage.WAIT)
        self.assertEqual((msg.body, msg.conversation_id), (messaging.DEFAULT_WAIT_MESSAGE, 42))
        self.assertEqual(messaging.plan_wait.call(self.lead.pk), "whatsapp:skipped")  # same review: not again
        Tenant.objects.filter(pk=self.tenant.pk).update(wait_message_enabled=False)
        self.assertEqual(messaging.plan_wait.call(self.lead.pk), "mensagem de espera desligada")

    def test_custom_waiting_text_and_email_events_in_the_ais_context(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(wait_message="Oi {primeiro_nome}, já te respondemos!")
        Lead.objects.filter(pk=self.lead.pk).update(waiting_since=timezone.now(), frozen_at=timezone.now())
        messaging.plan_wait.call(self.lead.pk)
        self.assertEqual(OutboundMessage.objects.get().body, "Oi Bia, já te respondemos!")
        LeadEvent.objects.create(lead=self.lead, tenant=self.tenant, type=EventType.EMAIL_CLICKED, actor="contato",
                                 data={"url": "https://alto.example/tabela"})
        self.assertIn("clicou num link do e-mail (https://alto.example/tabela)", routing.lead_text(self.reload()))
