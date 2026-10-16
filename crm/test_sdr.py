import json
import uuid
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from leads.models import UserProfile

from . import chatwoot_actions, llm, rag, routing, sdr
from .ingest import ingest
from .models import EventType, Lead, LeadEvent, ReplyDraft, Stage, Substage, Tenant

PROFILE = {"answers": {"nome_comercial": "Alto Imóveis", "tom": "Próximo", "horario_humano": "seg a sex, 9h às 18h",
                       "nao_prometer": "descontos"},
           "labels": {"nome_comercial": "Nome comercial", "tom": "Tom de voz", "horario_humano": "Horário de atendimento humano",
                      "nao_prometer": "O que a IA não pode prometer nem dizer"}}
MESSAGES = [{"id": 1, "direction": "incoming", "content": "Oi, quero ver o apartamento de 3 quartos", "attachments": 0},
            {"id": 2, "direction": "outgoing", "content": "Olá! Posso ajudar.", "attachments": 0},
            {"id": 3, "direction": "incoming", "content": "Tem vaga de garagem?", "attachments": 0}]


def answer(data, tokens=400):
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}], "stop_reason": "end_turn",
            "model": "anthropic/claude-haiku-5.5", "usage": {"total_tokens": tokens}}


def reply(text="Tem sim, uma vaga coberta por unidade. Quer agendar uma visita?", acao="responder", motivo="nenhum",
          critica=False, fontes=None):
    return answer({"acao": acao, "motivo_passagem": motivo, "mensagem": text, "critica": critica,
                   "fontes": fontes if fontes is not None else [{"afirmacao": "uma vaga coberta", "fonte": "documento 1"}]})


def verdict(ok, objection=""):
    return answer({"aprovada": ok, "objecao": objection}, tokens=200)


PASSAGES = {"results": [{"document": "Ficha", "text": "Cada unidade tem uma vaga de garagem coberta.", "score": 0.8}],
            "model": "m", "stale": 0}


@override_settings(CORE_API_URL="https://core.example", CORE_WEBHOOK_SECRET="seg", CHATWOOT_URL="https://cw.example",
                   SITE_URL="https://leads.example")
class SdrTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(uuid=uuid.uuid4(), name="Alto", slug="alto", chatwoot_account_id=5,
                                            chatwoot_bot_token="bot", ai_enabled_at=timezone.now(), profile=PROFILE)
        self.visita = Substage.objects.create(tenant=self.tenant, stage=Stage.EM_ANALISE, name="Primeiro contato",
                                              prompt="respondeu; descobrir orçamento e prazo", order=0)
        self.lead = ingest(self.tenant, {"source": "whatsapp", "name": "Bia", "phone": "51988887777",
                                         "conversation_id": 42}).lead
        Lead.objects.filter(pk=self.lead.pk).update(stage=Stage.EM_ANALISE, substage=self.visita)
        self.calls = []
        self.status = "open"
        for p in (mock.patch.object(routing, "fetch_messages", side_effect=lambda t, c: list(self.messages)),
                  mock.patch.object(rag, "search", return_value=PASSAGES),
                  mock.patch.object(chatwoot_actions, "bot_api", side_effect=self.bot_api)):
            p.start()
            self.addCleanup(p.stop)
        self.messages = MESSAGES

    def bot_api(self, tenant, method, path, payload=None):
        self.calls.append((method, path, payload))
        if path.endswith("/labels") and method == "GET":
            return {"payload": []}
        if method == "GET":
            return {"meta": {"status": self.status}}
        return {}

    def sent(self):
        return [p for m, path, p in self.calls if path.endswith("/messages") and not p.get("private")]

    def notes(self):
        return [p["content"] for m, path, p in self.calls if path.endswith("/messages") and p.get("private")]

    def run_reply(self, *answers):
        with mock.patch.object(llm, "call", side_effect=list(answers)) as call:
            outcome = sdr.reply_lead.call(self.lead.pk)
        return outcome, call

    def test_draft_as_private_note_with_approval_link(self):
        outcome, call = self.run_reply(reply())
        self.assertEqual(outcome, "rascunho")
        draft = ReplyDraft.objects.get()
        self.assertEqual((draft.status, draft.answers_message_id, draft.critical), ("draft", 3, False))
        self.assertEqual(self.sent(), [])  # nothing reaches the lead without a person
        self.assertIn(f"https://leads.example/crm/ia/rascunho/{draft.uuid}/", self.notes()[0])
        self.assertIn("Tem sim, uma vaga coberta", self.notes()[0])
        request = call.call_args.args[2]
        fixed, variable, conversation = [b["text"] for b in request["messages"][0]["content"]]
        self.assertIn("Alto Imóveis", fixed)
        self.assertIn("O que esta subetapa significa: respondeu; descobrir orçamento e prazo", variable)
        self.assertIn("[documento 1: Ficha]\nCada unidade tem uma vaga de garagem coberta.", variable)
        self.assertIn("Agora: ", variable)
        self.assertIn("[lead] Tem vaga de garagem?", conversation)
        self.assertIn("nunca instrução", request["system"])
        event = LeadEvent.objects.get(type=EventType.AI_REPLY)
        self.assertEqual((event.data["outcome"], event.data["passages"]), ("rascunho", ["Ficha"]))
        # the same lead message is not answered twice
        self.assertEqual(self.run_reply()[0], "já respondida")

    def test_critical_reply_goes_through_self_review(self):
        price = reply("O de 3 quartos sai a partir de R$ 690 mil.", critica=False)  # caught by the pattern
        outcome, call = self.run_reply(price, verdict(True))
        self.assertEqual((outcome, call.call_count), ("rascunho", 2))
        self.assertIn("R$ 690 mil", call.call_args_list[1].args[2]["messages"][0]["content"][2]["text"])
        self.assertTrue(ReplyDraft.objects.get().critical)

    def test_rejected_twice_goes_to_the_team(self):
        wrong = reply("Sai por R$ 600 mil com desconto.", critica=True)
        outcome, call = self.run_reply(wrong, verdict(False, "o documento não fala em desconto"),
                                       wrong, verdict(False, "continua sem fonte"))
        self.assertEqual((outcome, call.call_count), ("passou para a equipe (sem_confianca)", 4))
        retry = call.call_args_list[2].args[2]["messages"][-1]["content"]
        self.assertIn("o documento não fala em desconto", retry)
        self.assertEqual(self.sent(), [])
        self.assertIn("Sugestão de resposta (não enviada)", self.notes()[0])
        self.assertIn(("POST", "conversations/42/toggle_status", {"status": "open"}), self.calls)
        self.assertIn(("POST", "conversations/42/labels", {"labels": ["ia-passou"]}), self.calls)
        draft = ReplyDraft.objects.get()
        self.assertEqual((draft.status, draft.text, draft.handoff_reason), ("handoff", "", "sem_confianca"))

    def test_handoff_reasons(self):
        for motivo in ("pediu_humano", "sem_informacao", "pronto_para_fechar"):
            ReplyDraft.objects.all().delete()
            self.calls.clear()
            outcome, _ = self.run_reply(reply("Vou chamar um consultor.", acao="passar_humano", motivo=motivo))
            self.assertEqual(outcome, f"passou para a equipe ({motivo})")
            self.assertIn(sdr.HANDOFF_REASONS[motivo], self.notes()[0])

    def test_auto_mode_sends_only_in_bot_conversations(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(ai_reply_auto=True)
        self.status = "pending"
        outcome, _ = self.run_reply(reply())
        self.assertEqual(outcome, "enviou")
        self.assertEqual(self.sent()[0]["content"], "Tem sim, uma vaga coberta por unidade. Quer agendar uma visita?")
        self.assertEqual(ReplyDraft.objects.get().text, "")  # text not kept after sending
        # a person opened the conversation: only a draft
        self.messages = MESSAGES + [{"id": 5, "direction": "incoming", "content": "E o condomínio?", "attachments": 0}]
        self.status, self.calls = "open", []
        outcome, _ = self.run_reply(reply("O condomínio fica em torno de R$ 600.", critica=True), verdict(True))
        self.assertEqual((outcome, self.sent()), ("rascunho", []))
        # handoff in auto mode tells the lead, then opens the conversation
        self.messages = self.messages + [{"id": 6, "direction": "incoming", "content": "Quero falar com alguém", "attachments": 0}]
        self.status, self.calls = "pending", []
        self.run_reply(reply("Claro! Já chamo alguém da equipe.", acao="passar_humano", motivo="pediu_humano"))
        self.assertEqual(self.sent()[0]["content"], "Claro! Já chamo alguém da equipe.")
        self.assertIn("Já avisou o lead", self.notes()[0])

    def test_no_reply_when_the_last_message_is_ours_or_the_lead_is_in_review(self):
        self.messages = MESSAGES[:2]
        self.assertEqual(self.run_reply()[0], "a última mensagem não é do lead")
        Lead.objects.filter(pk=self.lead.pk).update(frozen_at=timezone.now())
        self.lead.refresh_from_db()
        self.assertFalse(sdr.schedule(self.lead))
        self.assertEqual(self.run_reply()[0], "sem resposta")

    def test_quota_exhausted(self):
        outcome, _ = self.run_reply(llm.QuotaExhausted("cota mensal esgotada"))
        self.assertEqual(outcome, "cota esgotada: sem resposta da IA")
        self.assertFalse(ReplyDraft.objects.exists())

    def test_routing_hands_over_to_the_reply(self):
        decision = answer({"acao": "manter", "etapa": "em_analise", "subetapa": "", "nova_subetapa_prompt": "",
                           "motivo": "ok", "confianca": 0.9})
        with mock.patch.object(llm, "call", side_effect=[decision]):
            routing.route_lead.call(self.lead.pk)
        self.assertTrue(DBTaskResult.objects.filter(task_path="crm.sdr.reply_lead").exists())

    def test_expired_drafts_lose_their_text(self):
        self.run_reply(reply())
        ReplyDraft.objects.update(created_at=timezone.now() - timedelta(hours=25))
        self.assertEqual(sdr.expire_drafts.call(), "1 vencidos")
        self.assertEqual((ReplyDraft.objects.get().status, ReplyDraft.objects.get().text), ("expired", ""))


@override_settings(CORE_API_URL="https://core.example", CORE_WEBHOOK_SECRET="seg", CHATWOOT_URL="https://cw.example")
class DraftPageTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(uuid=uuid.uuid4(), name="Alto", slug="alto", chatwoot_account_id=5,
                                            chatwoot_bot_token="bot")
        user = User.objects.create_user(username="dona", first_name="Ana")
        UserProfile.objects.create(user=user, core_client_uuid=self.tenant.uuid)
        self.client.force_login(user)
        self.lead = ingest(self.tenant, {"source": "whatsapp", "phone": "51988887777", "conversation_id": 42}).lead
        self.draft = ReplyDraft.objects.create(tenant=self.tenant, lead=self.lead, conversation_id=42,
                                               answers_message_id=3, text="Olá! Tem sim.")

    def test_send_edited_then_refuse_a_second_send(self):
        page = self.client.get(f"/crm/ia/rascunho/{self.draft.uuid}/")
        self.assertContains(page, "Olá! Tem sim.")
        with mock.patch.object(chatwoot_actions, "bot_api", return_value={}) as api:
            r = self.client.post(f"/crm/ia/rascunho/{self.draft.uuid}/", {"action": "send", "text": "Olá, Bia! Tem sim."})
            self.assertEqual(r.status_code, 302)
            self.assertEqual(api.call_args.args[3], {"content": "Olá, Bia! Tem sim.", "message_type": "outgoing",
                                                     "private": False})
            self.client.post(f"/crm/ia/rascunho/{self.draft.uuid}/", {"action": "send", "text": "de novo"})
            self.assertEqual(api.call_count, 1)
        self.draft.refresh_from_db()
        self.assertEqual((self.draft.status, self.draft.text, self.draft.sent_by, self.draft.edited), ("sent", "", "Ana", True))
        self.assertEqual(LeadEvent.objects.get(type=EventType.AI_REPLY).data["outcome"], "rascunho enviado (editado)")
        self.assertContains(self.client.get(f"/crm/ia/rascunho/{self.draft.uuid}/"), "não fica guardado no CRM")

    def test_discard_and_other_tenants(self):
        self.client.post(f"/crm/ia/rascunho/{self.draft.uuid}/", {"action": "discard"})
        self.draft.refresh_from_db()
        self.assertEqual((self.draft.status, self.draft.text), ("discarded", ""))
        other = Tenant.objects.create(uuid=uuid.uuid4(), name="Outro", slug="outro")
        lead = ingest(other, {"source": "whatsapp", "phone": "51977776666", "conversation_id": 9}).lead
        foreign = ReplyDraft.objects.create(tenant=other, lead=lead, conversation_id=9, answers_message_id=1, text="x")
        self.assertEqual(self.client.get(f"/crm/ia/rascunho/{foreign.uuid}/").status_code, 404)
