import uuid
from datetime import timedelta

from django.contrib.auth.models import User
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.utils import timezone

from leads.models import UserProfile

from . import funnel
from .ingest import ingest
from .models import EventType, Lead, LeadEvent, Stage, Substage, Tenant


def make_tenant(slug="pousada"):
    return Tenant.objects.create(uuid=uuid.uuid4(), name=slug.title(), slug=slug)


class MoveTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant()
        self.novo = Substage.objects.create(tenant=self.tenant, stage=Stage.CRIADO, name="Novo", order=0)
        self.contato = Substage.objects.create(tenant=self.tenant, stage=Stage.EM_ANALISE, name="Primeiro contato", order=0)
        self.visita = Substage.objects.create(tenant=self.tenant, stage=Stage.EM_ANALISE, name="Visita marcada", order=1)
        self.lead = ingest(self.tenant, {"source": "manual", "phone": "51999998888"}).lead

    def test_free_path_and_history(self):
        r = funnel.move(self.lead, substage=self.visita, actor="ana", reason="marcou visita")
        self.assertEqual((r.status, r.lead.stage, r.lead.substage), ("moved", Stage.EM_ANALISE, self.visita))
        e = LeadEvent.objects.get(type=EventType.STAGE_CHANGED)
        self.assertEqual((e.actor, e.reason, e.data["to_substage"], e.data["by"]), ("ana", "marcou visita", "Visita marcada", "human"))
        self.assertEqual(funnel.move(r.lead, substage=self.visita, actor="ana").status, "unchanged")

    def test_bare_stage_lands_on_first_active_substage(self):
        r = funnel.move(self.lead, Stage.EM_ANALISE, actor="ana")
        self.assertEqual(r.lead.substage, self.contato)
        self.contato.is_active = False
        self.contato.save()
        r = funnel.move(r.lead, Stage.CRIADO, actor="ana")
        r = funnel.move(r.lead, Stage.EM_ANALISE, actor="ana")
        self.assertEqual(r.lead.substage, self.visita)
        r = funnel.move(r.lead, Stage.QUALIFICADO, actor="ana")  # no sub-stages there
        self.assertIsNone(r.lead.substage)

    def test_destination_must_be_active_and_of_the_tenant(self):
        other = Substage.objects.create(tenant=make_tenant("outro"), stage=Stage.QUALIFICADO, name="X")
        self.visita.is_active = False
        self.visita.save()
        for bad in (other, self.visita, 999999):
            with self.assertRaises(funnel.FunnelError):
                funnel.move(self.lead, substage=bad, actor="ana")
        with self.assertRaises(funnel.FunnelError):
            funnel.move(self.lead, "inventada", actor="ana")

    def test_invalid_destination_from_auto_goes_to_review(self):
        r = funnel.move(self.lead, substage=999999, actor="llm", by=funnel.AUTO)
        self.assertEqual(r.status, "review")
        self.lead.refresh_from_db()
        self.assertIsNotNone(self.lead.frozen_at)
        self.assertTrue(LeadEvent.objects.filter(type=EventType.REVIEW).exists())

    def test_back_and_forth_lock(self):
        funnel.move(self.lead, substage=self.contato, actor="llm", by=funnel.AUTO)  # novo -> contato
        funnel.move(self.lead, substage=self.visita, actor="llm", by=funnel.AUTO)  # contato -> visita
        r = funnel.move(self.lead, substage=self.contato, actor="llm", reason="voltou a perguntar", by=funnel.AUTO)
        self.assertEqual(r.status, "review")
        self.assertIn("Vaivém", r.message)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.substage, self.visita)  # not applied
        self.assertIsNotNone(self.lead.frozen_at)
        # Frozen: no automatic move at all, even forward.
        self.assertEqual(funnel.move(self.lead, Stage.QUALIFICADO, actor="llm", by=funnel.AUTO).status, "frozen")
        # A person decides: applied (no lock for people) and released.
        r = funnel.move(self.lead, substage=self.contato, actor="ana", reason="decidi")
        self.assertEqual(r.status, "moved")
        self.assertIsNone(r.lead.frozen_at)
        self.assertTrue(LeadEvent.objects.filter(type=EventType.STAGE_CHANGED, data__released=True).exists())
        self.assertEqual(funnel.move(r.lead, Stage.QUALIFICADO, actor="llm", by=funnel.AUTO).status, "moved")

    def test_lock_looks_at_return_not_volume_and_only_24h(self):
        funnel.move(self.lead, substage=self.contato, actor="llm", by=funnel.AUTO)
        funnel.move(self.lead, substage=self.visita, actor="llm", by=funnel.AUTO)
        self.assertEqual(funnel.move(self.lead, Stage.QUALIFICADO, actor="llm", by=funnel.AUTO).status, "moved")
        LeadEvent.objects.update(created_at=timezone.now() - timedelta(hours=25))
        self.assertEqual(funnel.move(self.lead, substage=self.contato, actor="llm", by=funnel.AUTO).status, "moved")

    def test_release_without_moving(self):
        funnel.move(self.lead, substage=999999, actor="llm", by=funnel.AUTO)
        self.assertTrue(funnel.release(self.lead, actor="ana"))
        self.lead.refresh_from_db()
        self.assertIsNone(self.lead.frozen_at)
        self.assertFalse(funnel.release(self.lead, actor="ana"))

    def test_closed_at_and_reopen_rule(self):
        r = funnel.move(self.lead, Stage.PERDIDO, actor="ana")
        self.assertIsNotNone(r.lead.closed_at)
        ingest(self.tenant, {"source": "manual", "phone": "51999998888"})  # contact's new open lead
        with self.assertRaises(IntegrityError):
            funnel.move(r.lead, Stage.CRIADO, actor="ana")


@override_settings(AUTHENTICATION_BACKENDS=["django.contrib.auth.backends.ModelBackend"])
class FunnelScreensTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant()
        user = User.objects.create_user(username="dona")
        UserProfile.objects.create(user=user, core_client_uuid=self.tenant.uuid)
        self.client.force_login(user)
        self.lead = ingest(self.tenant, {"source": "manual", "name": "Bia", "phone": "51999998888"}).lead

    def test_create_edit_reorder_toggle(self):
        self.client.post("/crm/funil/subetapa/", {"stage": "em_analise", "name": "Primeiro contato", "prompt": "respondeu"})
        r = self.client.post("/crm/funil/subetapa/", {"stage": "em_analise", "name": "Visita"})
        self.assertContains(r, "Visita")
        first, second = Substage.objects.order_by("order")
        self.assertEqual((first.name, first.order, second.order), ("Primeiro contato", 0, 1))
        self.client.post(f"/crm/funil/subetapa/{second.pk}/ordem/", {"dir": "up"})
        self.assertEqual(list(Substage.objects.order_by("order").values_list("name", flat=True)), ["Visita", "Primeiro contato"])
        self.client.post("/crm/funil/subetapa/", {"id": second.pk, "name": "Visita marcada", "prompt": "agendou"})
        second.refresh_from_db()
        self.assertEqual((second.name, second.prompt), ("Visita marcada", "agendou"))
        self.client.post(f"/crm/funil/subetapa/{second.pk}/ativa/")
        second.refresh_from_db()
        self.assertFalse(second.is_active)

    def test_duplicate_name_is_explained(self):
        self.client.post("/crm/funil/subetapa/", {"stage": "criado", "name": "Novo"})
        self.assertContains(self.client.post("/crm/funil/subetapa/", {"stage": "criado", "name": "Novo"}), "Já existe")

    def test_other_tenants_substage_is_404(self):
        other = Substage.objects.create(tenant=make_tenant("outro"), stage=Stage.CRIADO, name="X")
        self.assertEqual(self.client.post(f"/crm/funil/subetapa/{other.pk}/ativa/").status_code, 404)
        self.assertEqual(self.client.post("/crm/funil/subetapa/", {"id": other.pk, "name": "Y"}).status_code, 404)
        r = self.client.post(f"/crm/leads/{self.lead.uuid}/etapa/", {"destination": f"sub:{other.pk}"})
        self.assertEqual(r.status_code, 400)

    def test_lead_page_moves_to_substage_and_releases(self):
        sub = Substage.objects.create(tenant=self.tenant, stage=Stage.QUALIFICADO, name="Quente")
        page = self.client.get(f"/crm/leads/{self.lead.uuid}/")
        self.assertContains(page, f'value="sub:{sub.pk}"')
        self.assertContains(page, 'value="stage:criado"')
        self.client.post(f"/crm/leads/{self.lead.uuid}/etapa/", {"destination": f"sub:{sub.pk}"})
        self.lead.refresh_from_db()
        self.assertEqual((self.lead.stage, self.lead.substage), (Stage.QUALIFICADO, sub))
        Lead.objects.filter(pk=self.lead.pk).update(frozen_at=timezone.now(), review_reason="Vaivém: teste")
        self.assertContains(self.client.get(f"/crm/leads/{self.lead.uuid}/"), "Em revisão humana")
        self.assertContains(self.client.get("/crm/", {"review": "1"}), "Revisão")
        self.client.post(f"/crm/leads/{self.lead.uuid}/liberar/")
        self.lead.refresh_from_db()
        self.assertIsNone(self.lead.frozen_at)


class FunnelBuildingTests(TestCase):
    """Decision of 2026-10-14: the LLM creates sub-stages and writes their
    prompts; at most 5 active per stage; prompts can be locked against the
    LLM; only people deactivate."""

    def setUp(self):
        self.tenant = make_tenant()

    def test_limit_of_five_active_per_stage_for_everyone(self):
        subs = [funnel.create_substage(self.tenant, Stage.QUALIFICADO, f"S{i}", by=Substage.LLM) for i in range(5)]
        for by in (Substage.LLM, Substage.HUMAN):
            with self.assertRaisesMessage(funnel.FunnelError, "5 subetapas ativas"):
                funnel.create_substage(self.tenant, Stage.QUALIFICADO, "Sexta", by=by)
        funnel.create_substage(self.tenant, Stage.PERDIDO, "Outra etapa")  # the limit is per stage
        funnel.set_substage_active(subs[0], False)
        sixth = funnel.create_substage(self.tenant, Stage.QUALIFICADO, "Sexta")
        with self.assertRaisesMessage(funnel.FunnelError, "5 subetapas ativas"):
            funnel.set_substage_active(subs[0], True)  # reactivating respects the limit too
        self.assertEqual(sixth.order, 5)

    def test_authorship_and_llm_limits(self):
        sub = funnel.create_substage(self.tenant, Stage.EM_ANALISE, "Visita", prompt="agendou", by=Substage.LLM)
        self.assertEqual((sub.created_by, sub.prompt_by, sub.prompt_locked), ("llm", "llm", False))
        funnel.update_substage(sub, prompt="agendou visita ao imóvel", by=Substage.LLM)
        for forbidden in ({"name": "X"}, {"message_text": "Oi"}, {"prompt_locked": False}):
            with self.assertRaises(funnel.FunnelError):
                funnel.update_substage(sub, by=Substage.LLM, **forbidden)
        funnel.update_substage(sub, prompt="escrito por uma pessoa", prompt_locked=True)
        sub.refresh_from_db()
        self.assertEqual((sub.prompt_by, sub.prompt_locked), ("human", True))
        with self.assertRaisesMessage(funnel.FunnelError, "bloqueado"):
            funnel.update_substage(sub, prompt="a IA tentando mudar", by=Substage.LLM)
        funnel.update_substage(sub, prompt_locked=False)
        funnel.update_substage(sub, prompt="a IA pode de novo", by=Substage.LLM)
        sub.refresh_from_db()
        self.assertEqual((sub.prompt, sub.prompt_by), ("a IA pode de novo", "llm"))

    def test_llm_cannot_lock_on_creation(self):
        sub = funnel.create_substage(self.tenant, Stage.CRIADO, "Novo", by=Substage.LLM, prompt_locked=True)
        self.assertFalse(sub.prompt_locked)

    def test_duplicate_and_empty_names(self):
        funnel.create_substage(self.tenant, Stage.CRIADO, "Novo")
        with self.assertRaisesMessage(funnel.FunnelError, "Já existe"):
            funnel.create_substage(self.tenant, Stage.CRIADO, "Novo", by=Substage.LLM)
        with self.assertRaises(funnel.FunnelError):
            funnel.create_substage(self.tenant, Stage.CRIADO, "  ")


@override_settings(AUTHENTICATION_BACKENDS=["django.contrib.auth.backends.ModelBackend"])
class FunnelScreenLimitsTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant()
        user = User.objects.create_user(username="dona")
        UserProfile.objects.create(user=user, core_client_uuid=self.tenant.uuid)
        self.client.force_login(user)

    def test_screen_shows_limit_lock_and_origin(self):
        for i in range(5):
            funnel.create_substage(self.tenant, Stage.CRIADO, f"S{i}", by=Substage.LLM, prompt="p")
        page = self.client.get("/crm/funil/")
        self.assertContains(page, "5/5 ativas")
        self.assertContains(page, "Limite de 5 subetapas ativas")
        self.assertContains(page, "criada pela IA")
        r = self.client.post("/crm/funil/subetapa/", {"stage": "criado", "name": "Sexta"})
        self.assertContains(r, "5 subetapas ativas")
        sub = Substage.objects.get(name="S0")
        self.client.post("/crm/funil/subetapa/", {"id": sub.pk, "name": "S0", "prompt": "meu", "prompt_locked": "on"})
        sub.refresh_from_db()
        self.assertEqual((sub.prompt, sub.prompt_by, sub.prompt_locked), ("meu", "human", True))
        self.assertContains(self.client.get("/crm/funil/"), "prompt bloqueado para a IA")
