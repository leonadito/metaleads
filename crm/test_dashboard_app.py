import hashlib
import hmac
import uuid

from django.test import TestCase, override_settings
from django.utils import timezone

from .ingest import ingest
from .models import ReplyDraft, Tenant


@override_settings(CHATWOOT_CRM_APP_SECRET="segredo", CHATWOOT_URL="https://cw.example")
class DashboardAppTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(uuid=uuid.uuid4(), name="Alto", slug="alto", chatwoot_account_id=5)
        self.code = hmac.new(b"segredo", str(self.tenant.uuid).encode(), hashlib.sha256).hexdigest()[:40]
        self.lead = ingest(self.tenant, {"source": "whatsapp", "name": "Bia", "phone": "51988887777",
                                         "conversation_id": 42}).lead
        self.base = f"/chatwoot/app/{self.tenant.uuid}/{self.code}/"

    def test_page_only_framed_by_chatwoot_and_only_with_the_right_code(self):
        r = self.client.get(self.base)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Security-Policy"], "frame-ancestors https://cw.example")
        self.assertNotIn("X-Frame-Options", r)
        self.assertContains(r, "https://cw.example")  # postMessage only from Chatwoot
        self.assertEqual(self.client.get(f"/chatwoot/app/{self.tenant.uuid}/{'0' * 40}/").status_code, 404)
        with override_settings(CHATWOOT_CRM_APP_SECRET=""):
            self.assertEqual(self.client.get(self.base).status_code, 404)

    def test_card_shows_the_lead_of_this_account_only(self):
        from .models import Lead

        Lead.objects.filter(pk=self.lead.pk).update(frozen_at=timezone.now(), review_reason="Confiança baixa (0.40)",
                                                    summary="Quer 3 quartos.")
        ReplyDraft.objects.create(tenant=self.tenant, lead=self.lead, conversation_id=42, answers_message_id=1, text="x")
        r = self.client.get(f"{self.base}conversa/42/")
        self.assertContains(r, "Bia")
        self.assertContains(r, "Em revisão humana")
        self.assertContains(r, "Confiança baixa (0.40)")
        self.assertContains(r, "Quer 3 quartos.")
        self.assertContains(r, "sugestão de resposta da IA")
        self.assertContains(self.client.get(f"{self.base}conversa/999/"), "ainda não tem lead")
        other = Tenant.objects.create(uuid=uuid.uuid4(), name="Outro", slug="outro", chatwoot_account_id=6)
        ingest(other, {"source": "whatsapp", "name": "Zeca", "phone": "51977776666", "conversation_id": 43})
        self.assertNotContains(self.client.get(f"{self.base}conversa/43/"), "Zeca")  # another tenant's conversation
        other_code = hmac.new(b"segredo", str(other.uuid).encode(), hashlib.sha256).hexdigest()[:40]
        self.assertEqual(self.client.get(f"/chatwoot/app/{other.uuid}/{self.code}/conversa/43/").status_code, 404)
        self.assertContains(self.client.get(f"/chatwoot/app/{other.uuid}/{other_code}/conversa/43/"), "Zeca")
