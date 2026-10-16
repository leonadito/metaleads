import json
import uuid
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from . import funnel, llm, routing
from .chatwoot import process_message
from .ingest import ingest
from .models import EventType, Lead, LeadEvent, Stage, Substage, Tenant

MESSAGES = [{"id": 1, "direction": "incoming", "content": "Oi, quero ver o apartamento de 3 quartos", "attachments": 0},
            {"id": 2, "direction": "outgoing", "content": "Claro! Quando pode visitar?", "attachments": 0},
            {"id": 3, "direction": "incoming", "content": "Sábado às 10h", "attachments": 0}]


def decision(acao="mover", etapa="em_analise", subetapa="Visita marcada", confianca=0.9, prompt="", motivo="Agendou visita"):
    return {"acao": acao, "etapa": etapa, "subetapa": subetapa, "nova_subetapa_prompt": prompt, "motivo": motivo,
            "confianca": confianca}


def answer(data, tokens=500):
    return {"content": [{"type": "text", "text": json.dumps(data)}], "stop_reason": "end_turn", "model": "anthropic/claude-haiku-5.5",
            "usage": {"total_tokens": tokens}}


@override_settings(CORE_API_URL="https://core.example", CORE_WEBHOOK_SECRET="seg")
class RoutingTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(uuid=uuid.uuid4(), name="Alto", slug="alto", chatwoot_account_id=5,
                                            ai_enabled_at=timezone.now())
        self.contato = Substage.objects.create(tenant=self.tenant, stage=Stage.EM_ANALISE, name="Primeiro contato", prompt="respondeu", order=0)
        self.visita = Substage.objects.create(tenant=self.tenant, stage=Stage.EM_ANALISE, name="Visita marcada", prompt="agendou visita", order=1)
        self.lead = ingest(self.tenant, {"source": "whatsapp", "name": "Bia", "phone": "51988887777", "conversation_id": 42,
                                         "form_answers": {"quartos": "3"}}).lead
        patcher = mock.patch.object(routing, "fetch_messages", return_value=MESSAGES)
        patcher.start()
        self.addCleanup(patcher.stop)

    def route(self, *answers):
        with mock.patch.object(llm, "call", side_effect=list(answers)) as call:
            outcome = routing.route_lead.call(self.lead.pk)
        self.lead.refresh_from_db()
        return outcome, call

    def test_moves_and_records_the_decision(self):
        outcome, call = self.route(answer(decision()))
        self.assertEqual(outcome, "moveu")
        self.assertEqual((self.lead.stage, self.lead.substage), (Stage.EM_ANALISE, self.visita))
        moved = LeadEvent.objects.get(lead=self.lead, type=EventType.STAGE_CHANGED)
        self.assertEqual((moved.actor, moved.data["by"], moved.reason), ("IA", "auto", "Agendou visita"))
        ai = LeadEvent.objects.get(lead=self.lead, type=EventType.AI_DECISION)
        self.assertEqual((ai.data["outcome"], ai.data["attempts"], ai.data["tokens"]), ("moveu", 1, 500))
        self.assertEqual((ai.data["self_review"], ai.data["truncated"]), ([], False))
        self.assertTrue(DBTaskResult.objects.filter(task_path="crm.summary.summarize_lead").exists())
        tenant_arg, task, request = call.call_args.args
        self.assertEqual(task, "sdr")
        user_parts = [p["text"] for p in request["messages"][0]["content"]]
        self.assertIn('subetapa "Visita marcada": agendou visita', user_parts[0])  # the funnel with prompts
        self.assertIn("<formulario>\n- quartos: 3\n</formulario>", user_parts[1])
        self.assertIn("<conversa>\n[lead] Oi, quero ver o apartamento de 3 quartos\n[empresa] Claro! Quando pode visitar?\n[lead] Sábado às 10h\n</conversa>", user_parts[2])
        self.assertIn("nunca instruções", request["system"])
        self.assertEqual(request["output_config"]["format"]["type"], "json_schema")

    def test_wrong_destination_goes_back_with_the_error_then_review(self):
        outcome, call = self.route(answer(decision(subetapa="Inventada")), answer(decision()))
        self.assertEqual((outcome, call.call_count), ("moveu", 2))
        retry = call.call_args_list[1].args[2]["messages"]
        self.assertIn('a subetapa "Inventada" não existe na etapa em_analise', retry[-1]["content"])
        LeadEvent.objects.all().delete()
        self.lead.substage, self.lead.stage = None, Stage.CRIADO
        Lead.objects.filter(pk=self.lead.pk).update(stage=Stage.CRIADO, substage=None)
        bad = answer(decision(etapa="em_analise", subetapa="Nope"))
        outcome, call = self.route(bad, bad, bad)
        self.assertEqual((outcome, call.call_count), ("revisão (destino inválido)", 3))
        self.assertIsNotNone(self.lead.frozen_at)

    def test_low_confidence_goes_to_review_without_self_review(self):
        outcome, call = self.route(answer(decision(confianca=0.5)))
        self.assertEqual((outcome, call.call_count), ("revisão (confiança baixa)", 1))
        self.assertIn("Confiança baixa (0.50)", self.lead.review_reason)
        funnel.release(self.lead, actor="ana")
        outcome, call = self.route(answer(decision(etapa="perdido", subetapa="", confianca=0.4)))
        self.assertEqual((outcome, call.call_count, self.lead.stage), ("revisão (confiança baixa)", 1, Stage.CRIADO))

    def test_critical_move_applied_when_the_self_review_approves(self):
        Lead.objects.filter(pk=self.lead.pk).update(summary_facts=[
            {"campo": "interesse", "texto": "Quer apartamento de 3 quartos", "mensagens": [1]},
            {"campo": "combinado", "texto": "Fechou a compra", "mensagens": [3]}])
        self.lead.refresh_from_db()
        approved = answer({"aprovada": True, "objecao": "", "fatos_divergentes": [2]}, tokens=300)
        outcome, call = self.route(answer(decision(etapa="convertido", subetapa="", motivo="Fechou")), approved)
        self.assertEqual((outcome, call.call_count, self.lead.stage), ("moveu", 2, Stage.CONVERTIDO))
        review = call.call_args_list[1].args[2]
        facts = review["messages"][0]["content"][1]["text"]
        self.assertIn("2. [combinado] Fechou a compra\n   origem: #3: Sábado às 10h", facts)
        self.assertIn("#3 [lead] Sábado às 10h", review["messages"][0]["content"][2]["text"])
        ai = LeadEvent.objects.get(lead=self.lead, type=EventType.AI_DECISION)
        self.assertEqual((ai.data["tokens"], ai.data["self_review"][0]["aprovada"], ai.data["divergent_facts"]), (800, True, 1))
        # the fact that did not match its source is dropped and the summary is rebuilt next time
        self.assertEqual([f["texto"] for f in self.lead.summary_facts], ["Quer apartamento de 3 quartos"])
        self.assertEqual(self.lead.summary_position, "")

    def test_critical_move_rejected_goes_back_then_to_review(self):
        rejected = answer({"aprovada": False, "objecao": "O lead só pediu uma visita, não comprou.", "fatos_divergentes": []})
        lost = answer(decision(etapa="convertido", subetapa="", motivo="Comprou"))
        outcome, call = self.route(lost, rejected, lost, rejected, lost, rejected)
        self.assertEqual((outcome, call.call_count), ("revisão (autocrítica reprovou)", 6))
        retry = call.call_args_list[2].args[2]["messages"][-1]["content"]
        self.assertIn("a revisão reprovou a decisão: O lead só pediu uma visita", retry)
        self.assertIn("A autocrítica reprovou a decisão em 3 tentativas", self.lead.review_reason)
        self.assertEqual(self.lead.stage, Stage.CRIADO)
        # a rejection followed by a better decision is applied
        funnel.release(self.lead, actor="ana")
        outcome, call = self.route(lost, rejected, answer(decision()))
        self.assertEqual((outcome, call.call_count, self.lead.substage), ("moveu", 3, self.visita))

    def test_creates_a_sub_stage_when_none_fits(self):
        outcome, _ = self.route(answer(decision(acao="criar_subetapa", etapa="qualificado", subetapa="Aguardando aprovação do banco",
                                                prompt="Pediu financiamento e espera a resposta do banco.")))
        self.assertEqual(outcome, "moveu")
        sub = Substage.objects.get(name="Aguardando aprovação do banco")
        self.assertEqual((sub.created_by, self.lead.substage), ("llm", sub))

    def test_keep_does_nothing_but_is_recorded(self):
        outcome, _ = self.route(answer(decision(acao="manter", etapa="criado", subetapa="")))
        self.assertEqual((outcome, self.lead.stage), ("manteve", Stage.CRIADO))
        self.assertEqual(LeadEvent.objects.filter(type=EventType.AI_DECISION).count(), 1)

    def test_quota_exhausted_leaves_the_lead_manual(self):
        outcome, _ = self.route(llm.QuotaExhausted("cota mensal de IA esgotada para este app"))
        self.assertEqual(outcome, "cota esgotada: lead no manual")
        self.tenant.refresh_from_db()
        self.assertIn("cota", self.tenant.ai_last_error)
        self.assertFalse(LeadEvent.objects.filter(type=EventType.AI_DECISION).exists())

    def test_not_routed_when_frozen_or_ai_off(self):
        Lead.objects.filter(pk=self.lead.pk).update(frozen_at=timezone.now())
        outcome, call = self.route()
        self.assertEqual((outcome, call.call_count), ("em revisão: só o resumo", 0))
        self.assertTrue(DBTaskResult.objects.filter(task_path="crm.summary.summarize_lead").exists())
        Lead.objects.filter(pk=self.lead.pk).update(frozen_at=None)
        Tenant.objects.filter(pk=self.tenant.pk).update(ai_enabled_at=None)
        self.assertEqual(self.route()[0], "não roteado")

    def test_schedule_once_per_burst(self):
        self.assertTrue(routing.schedule(self.lead))
        self.assertFalse(routing.schedule(self.lead))
        task = DBTaskResult.objects.get(task_path="crm.routing.route_lead")
        self.assertGreater(task.run_after, timezone.now())

    def test_incoming_chatwoot_message_schedules_routing(self):
        payload = {"event": "message_created", "id": 77, "account": {"id": 5}, "message_type": "incoming", "private": False,
                   "content": "Oi", "conversation": {"id": 42, "channel": "Channel::Whatsapp"},
                   "sender": {"id": 9, "name": "Bia", "phone_number": "+5551988887777", "type": "contact"}}
        process_message.call(self.tenant.pk, payload)
        self.assertTrue(DBTaskResult.objects.filter(task_path="crm.routing.route_lead").exists())

    def test_long_conversations_keep_the_latest_messages(self):
        many = [{"direction": "incoming", "content": "x" * 1000, "attachments": 0} for _ in range(100)]
        text = routing.conversation_text(many)
        self.assertTrue(text.startswith("[mensagens mais antigas omitidas]"))
        self.assertLessEqual(len(text), routing.CONVERSATION_CHAR_CAP + 100)
