import json
import uuid
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from leads.models import UserProfile

from . import funnel, funnel_ai, llm
from .models import Stage, StagePrompt, Substage, Tenant

PROFILE = {"version": 3, "ready_for_ai": True, "labels": {"ramo": "Ramo", "qualifica": "O que qualifica um lead"},
           "answers": {"ramo": "Imobiliária", "qualifica": "Tem entrada e quer comprar em até 6 meses",
                       "faq": [{"q": "Financia?", "a": "Sim"}]}}


def proposal(**overrides):
    base = {s: {"prompt_mestre": f"Mestre de {s}.", "subetapas": []} for s in Stage.values}
    base["em_analise"]["subetapas"] = [{"nome": "Primeiro contato", "prompt": "Respondeu à primeira mensagem."},
                                       {"nome": "Visita marcada", "prompt": "Agendou visita ao decorado."}]
    base.update(overrides)
    return base


def answer(data):
    return {"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": json.dumps(data)}],
            "stop_reason": "end_turn", "usage": {"total_tokens": 1}}


@override_settings(CORE_API_URL="https://core.example", CORE_WEBHOOK_SECRET="seg")
class FunnelAITests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(uuid=uuid.uuid4(), name="Alto", slug="alto", profile=PROFILE, profile_version=3,
                                            ai_enabled_at=timezone.now())

    def run_build(self, data=None, error=None):
        with mock.patch.object(llm, "call", side_effect=error, return_value=answer(data or proposal())) as call:
            result = funnel_ai.build_funnel.call(self.tenant.pk)
        return result, call

    def test_builds_master_prompts_and_substages_through_the_funnel_rules(self):
        result, call = self.run_build()
        self.assertEqual((result["prompts_mestres"], result["criadas"]), (6, 2))
        tenant_arg, task, request = call.call_args.args
        self.assertEqual(task, "sdr")
        self.assertIn("Tem entrada e quer comprar em até 6 meses", request["messages"][0]["content"])
        self.assertEqual(request["output_config"]["format"]["type"], "json_schema")
        self.assertNotIn("model", request)
        masters = funnel.stage_prompts(self.tenant)
        self.assertEqual((masters["qualificado"]["prompt"], masters["qualificado"]["by"]), ("Mestre de qualificado.", "llm"))
        self.assertEqual(StagePrompt.objects.get(stage="qualificado").profile_version, 3)
        sub = Substage.objects.get(name="Visita marcada")
        self.assertEqual((sub.stage, sub.created_by, sub.prompt_by), ("em_analise", "llm", "llm"))
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.ai_funnel_profile_version, 3)

    def test_respects_locks_existing_names_and_the_limit(self):
        funnel.set_stage_prompt(self.tenant, "criado", "Escrito pela dona.", prompt_locked=True)
        kept = funnel.create_substage(self.tenant, "em_analise", "Primeiro contato", prompt="da pessoa", prompt_locked=True)
        many = proposal(perdido={"prompt_mestre": "x", "subetapas": [{"nome": f"S{i}", "prompt": "p"} for i in range(7)]})
        result, _ = self.run_build(many)
        self.assertEqual(funnel.stage_prompts(self.tenant)["criado"]["prompt"], "Escrito pela dona.")
        kept.refresh_from_db()
        self.assertEqual(kept.prompt, "da pessoa")  # locked: untouched, not duplicated
        self.assertEqual(Substage.objects.filter(tenant=self.tenant, name__iexact="primeiro contato").count(), 1)
        self.assertEqual(Substage.objects.filter(tenant=self.tenant, stage="perdido").count(), 5)
        self.assertTrue(any("bloqueado" in i for i in result["ignoradas"]))

    def test_unlocked_existing_prompt_is_improved(self):
        sub = funnel.create_substage(self.tenant, "em_analise", "Visita marcada", prompt="velho")
        self.run_build()
        sub.refresh_from_db()
        self.assertEqual((sub.prompt, sub.prompt_by, sub.created_by), ("Agendou visita ao decorado.", "llm", "human"))

    def test_needs_ai_on_and_complete_profile_and_records_errors(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(ai_enabled_at=None)
        self.assertEqual(self.run_build()[0], "IA desligada")
        Tenant.objects.filter(pk=self.tenant.pk).update(ai_enabled_at=timezone.now(), profile={**PROFILE, "ready_for_ai": False})
        self.assertEqual(self.run_build()[0], "perfil incompleto")
        Tenant.objects.filter(pk=self.tenant.pk).update(profile=PROFILE)
        self.run_build(error=llm.QuotaExhausted("cota mensal de IA esgotada para este app"))
        self.tenant.refresh_from_db()
        self.assertIn("cota", self.tenant.ai_last_error)
        self.assertFalse(Substage.objects.exists())


@override_settings(CORE_API_URL="https://core.example", CORE_WEBHOOK_SECRET="seg")
class LLMClientTests(TestCase):
    def test_gateway_answers(self):
        tenant = Tenant.objects.create(uuid=uuid.uuid4(), name="Alto", slug="alto")
        ok = mock.Mock(status_code=200, json=lambda: answer({"a": 1}))
        with mock.patch("crm.llm.requests.post", return_value=ok) as post:
            self.assertEqual(llm.json_of(llm.call(tenant, "light", {"max_tokens": 10, "messages": []})), {"a": 1})
        self.assertTrue(post.call_args.args[0].endswith("/api/v1/llm/mensagens"))
        self.assertEqual(post.call_args.kwargs["json"]["tenant"], str(tenant.uuid))
        for status, exc in ((429, llm.QuotaExhausted), (409, llm.LLMError)):
            with mock.patch("crm.llm.requests.post", return_value=mock.Mock(status_code=status, json=lambda: {"error": "x"})):
                with self.assertRaises(exc):
                    llm.call(tenant, "light", {})


@override_settings(AUTHENTICATION_BACKENDS=["django.contrib.auth.backends.ModelBackend"])
class AISwitchTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(uuid=uuid.uuid4(), name="Alto", slug="alto")
        user = User.objects.create_user(username="dona")
        UserProfile.objects.create(user=user, core_client_uuid=self.tenant.uuid)
        self.client.force_login(user)

    def test_turning_on_needs_the_profile_and_queues_the_build(self):
        self.assertContains(self.client.get("/crm/funil/"), "Só dá para ligar com o perfil do cliente completo")
        self.assertContains(self.client.post("/crm/funil/ia/", {"action": "on"}), "perfil do cliente precisa estar completo")
        Tenant.objects.filter(pk=self.tenant.pk).update(profile=PROFILE, profile_version=3)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post("/crm/funil/ia/", {"action": "on"})
        self.tenant.refresh_from_db()
        self.assertIsNotNone(self.tenant.ai_enabled_at)
        from django_tasks_db.models import DBTaskResult

        self.assertTrue(DBTaskResult.objects.filter(task_path="crm.funnel_ai.build_funnel").exists())
        self.assertContains(self.client.get("/crm/funil/"), "Montando o funil")
        self.client.post("/crm/funil/ia/", {"action": "off"})
        self.tenant.refresh_from_db()
        self.assertIsNone(self.tenant.ai_enabled_at)


@override_settings(AUTHENTICATION_BACKENDS=["django.contrib.auth.backends.ModelBackend"])
class ProfileChangedNoticeTests(TestCase):
    def test_incomplete_profile_hides_the_review_button(self):
        tenant = Tenant.objects.create(uuid=uuid.uuid4(), name="Alto", slug="alto", ai_enabled_at=timezone.now(),
                                       ai_funnel_built_at=timezone.now(), ai_funnel_profile_version=3,
                                       profile={**PROFILE, "version": 4})
        user = User.objects.create_user(username="dona")
        UserProfile.objects.create(user=user, core_client_uuid=tenant.uuid)
        self.client.force_login(user)
        page = self.client.get("/crm/funil/")
        self.assertContains(page, "O perfil mudou (agora versão 4)")
        self.assertContains(page, "Revisar o funil com a IA")
        Tenant.objects.filter(pk=tenant.pk).update(profile={**PROFILE, "version": 5, "ready_for_ai": False})
        page = self.client.get("/crm/funil/")
        self.assertContains(page, "O perfil mudou e está incompleto; complete para revisar o funil com a IA.")
        self.assertNotContains(page, "Revisar o funil com a IA</button>")
