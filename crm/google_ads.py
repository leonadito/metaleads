"""Google Ads lead form webhook (Phase 4).

In the lead form, "Webhook integration": URL = /webhooks/google-ads/ and key
= a GoogleLeadFormKey created in the admin. Google POSTs one JSON per lead
(https://developers.google.com/google-ads/webhook/docs/implementation):
- `google_key` in the body is the only authentication; it names the tenant;
- `lead_id` dedupes (Google may deliver the same lead more than once);
- answers in `user_column_data` ({column_id, column_name, string_value});
  standard ids (FULL_NAME, EMAIL, PHONE_NUMBER...) go to the contact, every
  other answer to Lead.form_answers keyed by column_id, with the question
  text kept as its label;
- only ids of campaign / ad group / creative / form, no names;
- "Send test data" sends is_test=true: answered 200 and noted on the key,
  no lead is created.
Google expects 200 with "{}"; a 4xx is not retried, a 5xx is.
"""
import json
import logging

from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .ingest import IngestError, ingest
from .models import GoogleLeadFormKey, Source

logger = logging.getLogger(__name__)

CONTACT_COLUMNS = {"FULL_NAME", "FIRST_NAME", "LAST_NAME", "EMAIL", "WORK_EMAIL", "PHONE_NUMBER", "WORK_PHONE",
                   "CITY", "COMPANY_NAME", "COUNTRY"}

# Google's standard questions arrive without text (column_name is "N/A"):
# labels for the lead page.
STANDARD_LABELS = {
    "POSTAL_CODE": "CEP", "STREET_ADDRESS": "Endereço", "REGION": "Estado", "JOB_TITLE": "Cargo",
    "PHONE_NUMBER_VERIFIED": "Telefone verificado", "COMPANY_SIZE": "Tamanho da empresa",
    "PRODUCT": "Produto", "SERVICE": "Serviço", "OFFER": "Oferta", "CATEGORY": "Categoria",
    "PREFERRED_CONTACT_METHOD": "Forma de contato preferida", "PREFERRED_CONTACT_TIME": "Melhor horário para contato",
    "PREFERRED_LOCATION": "Local preferido", "PURCHASE_TIMELINE": "Prazo para a compra",
    "PROPERTY_TYPE": "Tipo de imóvel", "PRICE_RANGE": "Faixa de preço", "NUMBER_OF_BEDROOMS": "Número de quartos",
    "REALTOR_HELP_GOAL": "Objetivo (imóvel)", "PROPERTY_COMMUNITY": "Bairro / condomínio",
    "FURNISHED_PROPERTY": "Mobiliado", "PETS_ALLOWED_PROPERTY": "Aceita animais",
    "VEHICLE_MODEL": "Modelo do veículo", "VEHICLE_TYPE": "Tipo de veículo", "VEHICLE_CONDITION": "Novo ou usado",
    "VEHICLE_PURCHASE_TIMELINE": "Prazo para a compra do veículo", "VEHICLE_PAYMENT_TYPE": "Forma de pagamento",
    "PREFERRED_DEALERSHIP": "Concessionária preferida", "EDUCATION_PROGRAM": "Programa", "EDUCATION_COURSE": "Curso",
    "LEVEL_OF_EDUCATION": "Escolaridade", "DESTINATION_CITY": "Cidade de destino", "DEPARTURE_DATE": "Data de ida",
    "RETURN_DATE": "Data de volta", "NUMBER_OF_TRAVELERS": "Número de viajantes", "TRAVEL_BUDGET": "Orçamento da viagem",
}


def _ok():
    return JsonResponse({})


def _refuse(status, message):
    return JsonResponse({"message": message}, status=status)


def _id(value):
    """int64 ids; 0 means "not available" (only video/discovery ads have
    ad group and creative)."""
    return "" if value in (None, "", 0, "0") else str(value)


def to_ingest(payload):
    columns = {}
    labels = {}
    for item in payload.get("user_column_data") or []:
        column_id = str(item.get("column_id") or item.get("column_name") or "").strip()
        if not column_id:
            continue
        columns[column_id] = str(item.get("string_value") or "")
        label = item.get("column_name") or ""
        if label and label.upper() != "N/A":
            labels[column_id] = label
        elif column_id in STANDARD_LABELS:
            labels[column_id] = STANDARD_LABELS[column_id]
    name = columns.get("FULL_NAME") or " ".join(v for v in (columns.get("FIRST_NAME"), columns.get("LAST_NAME")) if v)
    country = columns.get("COUNTRY", "")
    raw = {k: v for k, v in payload.items() if k.lower() != "google_key"}
    raw["form_labels"] = {k: v for k, v in labels.items() if k not in CONTACT_COLUMNS}
    return {
        "source": Source.GOOGLE_ADS_FORM,
        "external_id": str(payload.get("lead_id") or "")[:128],
        "occurred_at": payload.get("lead_submit_time"),
        "name": name,
        "email": columns.get("EMAIL") or columns.get("WORK_EMAIL") or "",
        "phone": columns.get("PHONE_NUMBER") or columns.get("WORK_PHONE") or "",
        "city": columns.get("CITY", ""),
        "company": columns.get("COMPANY_NAME", ""),
        "country_code": country if len(country) == 2 else "",
        "form_answers": {k: v for k, v in columns.items() if k not in CONTACT_COLUMNS},
        "attribution": {
            "platform": "google",
            "kind": "form",
            "campaign_id": _id(payload.get("campaign_id")),
            "adset_id": _id(payload.get("adgroup_id")),
            "ad_id": _id(payload.get("creative_id")),
            "form_id": _id(payload.get("form_id")),
            "gclid": payload.get("gcl_id") or "",
            "raw": raw,
        },
    }


@csrf_exempt
@require_POST
def google_ads_webhook(request):
    try:
        payload = json.loads(request.body)
        if not isinstance(payload, dict):
            raise ValueError
    except ValueError:
        return _refuse(400, "corpo precisa ser um objeto JSON")
    # Some of Google's samples spell it "Google_key".
    raw_key = next((v for k, v in payload.items() if k.lower() == "google_key"), "")
    form_key = GoogleLeadFormKey.authenticate(str(raw_key or ""))
    if form_key is None:
        return _refuse(403, "google_key inválida")
    if payload.get("is_test"):
        GoogleLeadFormKey.objects.filter(pk=form_key.pk).update(last_test_at=timezone.now())
        return _ok()
    data = to_ingest(payload)
    if not data["external_id"]:
        return _refuse(400, "lead_id ausente")
    try:
        with transaction.atomic():
            ingest(form_key.tenant, data, actor=f"google-ads:{form_key.name}")
    except IngestError as exc:
        logger.warning("Lead %s do Google Ads recusado: %s", data["external_id"], exc)
        return _refuse(422, str(exc))
    GoogleLeadFormKey.objects.filter(pk=form_key.pk).update(last_lead_at=timezone.now())
    return _ok()
