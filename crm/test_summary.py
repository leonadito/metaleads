import json
import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from . import llm, routing, summary
from .ingest import ingest
from leads.models import UserProfile

from .models import Lead, Stage, Tenant

MESSAGES = [{"id": 10, "direction": "incoming", "content": "Oi, sou a Bia, de Canoas", "attachments": 0},
            {"id": 11, "direction": "outgoing", "content": "Olá Bia! Procura o quê?", "attachments": 0},
            {"id": 12, "direction": "incoming", "content": "Apartamento de 3 quartos até 600 mil", "attachments": 0}]


def answer(data):
    return {"content": [{"type": "text", "text": json.dumps(data)}], "stop_reason": "end_turn", "usage": {"total_tokens": 200}}


FIRST = {"resumo": "Bia, de Canoas, procura apartamento de 3 quartos até 600 mil.", "fatos": [
    {"campo": "dados", "texto": "Bia, de Canoas", "mensagens": [10]},
    {"campo": "interesse", "texto": "Apartamento de 3 quartos", "mensagens": [12]},
    {"campo": "orcamento_prazo", "texto": "Até 600 mil", "mensagens": [12, 99]},  # 99 was never given
    {"campo": "objecao", "texto": "Acha caro", "mensagens": [99]},  # no real source: dropped
    {"campo": "outro", "texto": "x", "mensagens": [10]},  # unknown field: dropped
]}


@override_settings(CORE_API_URL="https://core.example", CORE_WEBHOOK_SECRET="seg")
class SummaryTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(uuid=uuid.uuid4(), name="Alto", slug="alto", chatwoot_account_id=5,
                                            ai_enabled_at=timezone.now())
        self.lead = ingest(self.tenant, {"source": "whatsapp", "name": "Bia", "phone": "51988887777",
                                         "conversation_id": 42}).lead

    def run_summary(self, messages, *answers):
        with mock.patch.object(routing, "fetch_messages", return_value=messages), \
                mock.patch.object(llm, "call", side_effect=list(answers)) as call:
            outcome = summary.summarize_lead.call(self.lead.pk)
        self.lead.refresh_from_db()
        return outcome, call

    def test_first_summary_from_the_whole_conversation_keeps_only_sourced_facts(self):
        outcome, call = self.run_summary(MESSAGES, answer(FIRST))
        self.assertEqual(outcome, "refeito")
        tenant, task, request = call.call_args.args
        self.assertEqual(task, "light")
        self.assertIn("#10 [lead] Oi, sou a Bia, de Canoas\n#11 [empresa]", request["messages"][0]["content"])
        self.assertEqual(self.lead.summary_facts, [
            {"campo": "dados", "texto": "Bia, de Canoas", "mensagens": [10]},
            {"campo": "interesse", "texto": "Apartamento de 3 quartos", "mensagens": [12]},
            {"campo": "orcamento_prazo", "texto": "Até 600 mil", "mensagens": [12]}])
        self.assertEqual((self.lead.summary_message_id, self.lead.summary_since_rebuild), (12, 0))
        self.assertEqual(self.lead.summary_position, "criado:")

    def test_incremental_update_sends_only_the_new_messages(self):
        self.run_summary(MESSAGES, answer(FIRST))
        newer = MESSAGES + [{"id": 13, "direction": "incoming", "content": "Na verdade até 700 mil", "attachments": 0}]
        updated = {"resumo": "Orçamento subiu para 700 mil.", "fatos": [
            {"campo": "dados", "texto": "Bia, de Canoas", "mensagens": [10]},
            {"campo": "orcamento_prazo", "texto": "Até 700 mil", "mensagens": [12, 13]}]}
        outcome, call = self.run_summary(newer, answer(updated))
        self.assertEqual(outcome, "atualizado")
        body = call.call_args.args[2]["messages"][0]["content"]
        self.assertIn("#13 [lead] Na verdade até 700 mil", body)
        self.assertNotIn("#11", body.split("Mensagens novas:")[1])
        self.assertIn("- [orcamento_prazo] Até 600 mil (mensagens #12)", body)  # the current facts
        self.assertEqual(self.lead.summary_facts[1]["mensagens"], [12, 13])  # old id allowed, it was a source before
        self.assertEqual((self.lead.summary_message_id, self.lead.summary_since_rebuild), (13, 1))
        self.assertEqual(self.run_summary(newer)[0], "sem mensagens novas")

    def test_rebuilt_when_the_position_changes_or_after_many_messages(self):
        self.run_summary(MESSAGES, answer(FIRST))
        Lead.objects.filter(pk=self.lead.pk).update(stage=Stage.EM_ANALISE)
        newer = MESSAGES + [{"id": 13, "direction": "incoming", "content": "Pode ser sábado", "attachments": 0}]
        self.assertEqual(self.run_summary(newer, answer(FIRST))[0], "refeito")
        Lead.objects.filter(pk=self.lead.pk).update(summary_since_rebuild=summary.REBUILD_EVERY - 1)
        newer = newer + [{"id": 14, "direction": "incoming", "content": "Ok", "attachments": 0}]
        outcome, call = self.run_summary(newer, answer(FIRST))
        self.assertEqual(outcome, "refeito")
        self.assertIn("conversa inteira", call.call_args.args[2]["messages"][0]["content"])

    def test_quota_exhausted_keeps_the_old_summary(self):
        outcome, _ = self.run_summary(MESSAGES, llm.QuotaExhausted("cota mensal esgotada"))
        self.assertEqual((outcome, self.lead.summary), ("cota esgotada", ""))

    def test_routing_reads_the_summary_and_the_lead_page_shows_it(self):
        self.run_summary(MESSAGES, answer(FIRST))
        self.assertIn("- [interesse] Apartamento de 3 quartos (mensagens #12)", routing.lead_text(self.lead))
        user = get_user_model().objects.create_user(username="dona")
        UserProfile.objects.create(user=user, core_client_uuid=self.tenant.uuid)
        self.client.force_login(user)
        r = self.client.get(f"/crm/leads/{self.lead.uuid}/")
        self.assertContains(r, "Resumo da IA")
        self.assertContains(r, "Apartamento de 3 quartos")
        self.assertContains(r, "#12")
