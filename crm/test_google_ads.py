import json
import uuid

from django.test import TestCase

from .models import GoogleLeadFormKey, Lead, LeadAttribution, Tenant

KEY = "gk_chave-de-teste"


def payload(lead_id="TeSt-lead-1", is_test=None, key=KEY, extra=None):
    body = {
        "lead_id": lead_id,
        "api_version": "1.0",
        "form_id": 123456789012,
        "campaign_id": 987654321,
        "adgroup_id": 0,
        "creative_id": 0,
        "gcl_id": "Cj0KCQ-clique",
        "lead_submit_time": "2026-10-10T12:30:00Z",
        "google_key": key,
        "user_column_data": [
            {"column_name": "Full Name", "string_value": "Ana Souza", "column_id": "FULL_NAME"},
            {"column_name": "User Phone", "string_value": "+5551999998888", "column_id": "PHONE_NUMBER"},
            {"column_name": "User Email", "string_value": "Ana@Exemplo.com", "column_id": "EMAIL"},
            {"column_name": "City", "string_value": "Porto Alegre", "column_id": "CITY"},
            {"string_value": "Apartamento", "column_id": "PROPERTY_TYPE"},
            {"column_name": "Qual o seu orçamento?", "string_value": "500 mil", "column_id": "QUAL_O_SEU_ORCAMENTO"},
        ],
        **(extra or {}),
    }
    if is_test is not None:
        body["is_test"] = is_test
    return body


class GoogleAdsWebhookTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(uuid=uuid.uuid4(), name="Imobiliária", slug="imob")
        self.form_key = GoogleLeadFormKey(tenant=self.tenant, name="Form apartamentos")
        self.form_key.set_key(KEY)
        self.form_key.save()

    def post(self, body):
        return self.client.post("/webhooks/google-ads/", json.dumps(body), content_type="application/json")

    def test_lead_is_recorded(self):
        r = self.post(payload())
        self.assertEqual((r.status_code, r.json()), (200, {}))
        lead = Lead.objects.get()
        self.assertEqual((lead.tenant, lead.source, lead.external_id), (self.tenant, "google_ads_form", "TeSt-lead-1"))
        self.assertEqual(lead.created_at.isoformat(), "2026-10-10T12:30:00+00:00")
        self.assertEqual(lead.form_answers, {"PROPERTY_TYPE": "Apartamento", "QUAL_O_SEU_ORCAMENTO": "500 mil"})
        c = lead.contact
        self.assertEqual((c.name, c.email, c.phone_e164, c.city), ("Ana Souza", "ana@exemplo.com", "+5551999998888", "Porto Alegre"))
        a = LeadAttribution.objects.get()
        self.assertEqual((a.platform, a.kind, a.campaign_id, a.adset_id, a.ad_id, a.form_id, a.gclid),
                         ("google", "form", "987654321", "", "", "123456789012", "Cj0KCQ-clique"))
        self.assertNotIn("google_key", a.raw)
        self.assertEqual(a.raw["form_labels"], {"PROPERTY_TYPE": "Tipo de imóvel", "QUAL_O_SEU_ORCAMENTO": "Qual o seu orçamento?"})
        self.form_key.refresh_from_db()
        self.assertIsNotNone(self.form_key.last_lead_at)

    def test_duplicate_delivery_is_one_lead(self):
        self.post(payload())
        self.assertEqual(self.post(payload()).status_code, 200)
        self.assertEqual(Lead.objects.count(), 1)

    def test_google_test_button_creates_nothing(self):
        r = self.post(payload(is_test=True))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Lead.objects.exists())
        self.form_key.refresh_from_db()
        self.assertIsNotNone(self.form_key.last_test_at)

    def test_wrong_or_revoked_key(self):
        self.assertEqual(self.post(payload(key="outra")).status_code, 403)
        GoogleLeadFormKey.objects.update(revoked_at="2026-10-01T00:00:00Z")
        self.assertEqual(self.post(payload()).status_code, 403)
        self.assertFalse(Lead.objects.exists())

    def test_key_spelled_like_googles_samples(self):
        body = payload()
        body["Google_key"] = body.pop("google_key")
        self.assertEqual(self.post(body).status_code, 200)

    def test_bad_requests_are_4xx(self):
        self.assertEqual(self.client.post("/webhooks/google-ads/", "x", content_type="application/json").status_code, 400)
        self.assertEqual(self.post(payload(lead_id="")).status_code, 400)
        body = payload()
        body["user_column_data"] = [{"column_id": "FULL_NAME", "string_value": "Sem contato"}]
        self.assertEqual(self.post(body).status_code, 422)

    def test_first_and_last_name(self):
        body = payload()
        body["user_column_data"] = [
            {"column_id": "FIRST_NAME", "string_value": "Bia"}, {"column_id": "LAST_NAME", "string_value": "Lima"},
            {"column_id": "WORK_EMAIL", "string_value": "bia@empresa.com"},
        ]
        self.post(body)
        self.assertEqual((Lead.objects.get().contact.name, Lead.objects.get().contact.email), ("Bia Lima", "bia@empresa.com"))
