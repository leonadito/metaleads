import json
import uuid

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from leads.models import UserProfile

from .ingest import ingest
from .models import EventType, Lead, LeadEvent, Stage, Tenant


def make_account(name):
    tenant = Tenant.objects.create(uuid=uuid.uuid4(), name=name.title(), slug=name, chatwoot_account_id=5)
    user = User.objects.create_user(username=name, first_name="Ana")
    UserProfile.objects.create(user=user, core_client_uuid=tenant.uuid)
    return tenant, user


@override_settings(AUTHENTICATION_BACKENDS=["django.contrib.auth.backends.ModelBackend"], CHATWOOT_URL="https://chat.example")
class CrmScreensTests(TestCase):
    def setUp(self):
        self.tenant, self.user = make_account("pousada")
        self.other, _ = make_account("outro")
        self.lead = ingest(self.tenant, {
            "source": "agipages_form", "name": "Bia", "phone": "51999998888", "conversation_id": 9,
            "form_answers": {"quartos": "2", "interesses": ["praia", "serra"]},
            "landing_page": {"name": "Página Verão", "url": "https://exemplo.com/verao"},
            "attribution": {"platform": "meta", "kind": "landing_page", "utm_campaign": "verao",
                            "raw": {"form_labels": {"quartos": "Quantos quartos?"}}},
        }).lead
        self.foreign = ingest(self.other, {"source": "manual", "name": "Carla", "phone": "51988887777"}).lead
        self.client.force_login(self.user)

    def test_login_required(self):
        self.client.logout()
        self.assertEqual(self.client.get("/crm/").status_code, 302)

    def test_account_without_client_is_asked_to_pick_one(self):
        self.client.force_login(User.objects.create_user(username="equipe"))
        self.assertContains(self.client.get("/crm/"), "Escolha um cliente")

    def test_kanban_shows_only_own_leads(self):
        page = self.client.get("/crm/")
        self.assertContains(page, "Bia")
        self.assertContains(page, "Página Verão")
        self.assertNotContains(page, "Carla")

    def test_filters(self):
        self.assertContains(self.client.get("/crm/", {"q": "9999-8888"}), "Bia")
        self.assertNotContains(self.client.get("/crm/", {"q": "ninguém"}), "Bia")
        self.assertNotContains(self.client.get("/crm/", {"source": "manual"}), "Bia")
        partial = self.client.get("/crm/", {"source": "agipages_form"}, HTTP_HX_REQUEST="true")
        self.assertContains(partial, 'id="board"')
        self.assertNotContains(partial, "<html")

    def test_other_tenants_lead_is_404(self):
        self.assertEqual(self.client.get(f"/crm/leads/{self.foreign.uuid}/").status_code, 404)
        r = self.client.post(f"/crm/leads/{self.foreign.uuid}/etapa/", {"stage": "perdido"})
        self.assertEqual(r.status_code, 404)
        self.foreign.refresh_from_db()
        self.assertEqual(self.foreign.stage, Stage.CRIADO)

    def test_lead_page(self):
        page = self.client.get(f"/crm/leads/{self.lead.uuid}/")
        self.assertContains(page, "Quantos quartos?")  # agipages label
        self.assertContains(page, "praia, serra")
        self.assertContains(page, "https://wa.me/5551999998888")
        self.assertContains(page, "https://chat.example/app/accounts/5/conversations/9")
        self.assertContains(page, "verao")

    def test_stage_from_lead_page(self):
        r = self.client.post(f"/crm/leads/{self.lead.uuid}/etapa/", {"stage": "qualificado", "reason": "quer fechar"},
                             HTTP_HX_REQUEST="true")
        self.assertContains(r, 'id="lead-main"')
        self.assertIn("crm-toast", r["HX-Trigger"])
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage, Stage.QUALIFICADO)
        event = LeadEvent.objects.get(lead=self.lead, type=EventType.STAGE_CHANGED)
        self.assertEqual((event.actor, event.reason), ("Ana", "quer fechar"))

    def test_stage_from_kanban_asks_board_refresh(self):
        r = self.client.post(f"/crm/leads/{self.lead.uuid}/etapa/", {"stage": "em_analise", "from": "kanban"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(json.loads(r["HX-Trigger"])["board-changed"])

    def test_reopening_when_contact_has_another_open_lead_is_explained(self):
        Lead.objects.filter(pk=self.lead.pk).update(stage=Stage.PERDIDO)
        ingest(self.tenant, {"source": "manual", "phone": "51999998888"})  # new open lead, same contact
        r = self.client.post(f"/crm/leads/{self.lead.uuid}/etapa/", {"stage": "criado", "from": "kanban"})
        self.assertIn("outro lead aberto", json.loads(r["HX-Trigger"])["crm-toast"])
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage, Stage.PERDIDO)

    def test_invalid_stage(self):
        self.assertEqual(self.client.post(f"/crm/leads/{self.lead.uuid}/etapa/", {"stage": "x"}).status_code, 400)

    def test_note(self):
        r = self.client.post(f"/crm/leads/{self.lead.uuid}/nota/", {"text": "ligar amanhã"})
        self.assertContains(r, "ligar amanhã")
        self.assertTrue(LeadEvent.objects.filter(lead=self.lead, type=EventType.NOTE, reason="ligar amanhã").exists())

    def test_manual_entry(self):
        r = self.client.post("/crm/leads/novo/", {"name": "Dani", "phone": "(51) 97777-6666", "note": "indicação"})
        lead = Lead.objects.get(contact__name="Dani")
        self.assertRedirects(r, f"/crm/leads/{lead.uuid}/")
        self.assertEqual((lead.tenant, lead.source), (self.tenant, "manual"))
        self.assertEqual(LeadEvent.objects.get(lead=lead, type=EventType.CREATED).actor, "Ana")
        self.assertTrue(LeadEvent.objects.filter(lead=lead, type=EventType.NOTE, reason="indicação").exists())

    def test_manual_entry_of_known_contact_is_a_reentry(self):
        r = self.client.post("/crm/leads/novo/", {"name": "Bia", "phone": "51999998888"})
        self.assertRedirects(r, f"/crm/leads/{self.lead.uuid}/?existente=1", fetch_redirect_response=False)
        self.assertEqual(Lead.objects.filter(tenant=self.tenant).count(), 1)

    def test_manual_entry_needs_a_contact(self):
        self.assertContains(self.client.post("/crm/leads/novo/", {"name": "Sem contato"}), "Informe ao menos telefone ou e-mail")
