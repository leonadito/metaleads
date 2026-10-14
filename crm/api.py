"""Lead intake API: POST /api/v1/leads

Headers:
    Authorization: Bearer <token>          (crm.ApiToken, created by the
                                            create_api_token command: a tenant
                                            token, or a service token for the
                                            Agitare's own apps, which must also
                                            send "tenant": <core uuid>)
    Idempotency-Key: <unique per lead>     (required; a retry with the same
                                            key and body gets the same answer,
                                            the same key with another body 409)
Body (JSON): see crm/ingest.py and the "Desenho das tabelas" in the plan —
    name, email, phone, city, company, country_code, whatsapp_wa_id,
    instagram_id, messenger_psid, source, external_id, form_answers{},
    landing_page{id,name,url}, conversation_id, consent{whatsapp,email,source},
    attribution{platform, kind, campaign_id, ..., raw{}}
Answer: 201 when a lead was opened, 200 otherwise (re-entry or duplicate),
    {"lead": uuid, "contact": uuid, "created": bool, "duplicate": bool, "warnings": []}
"""
import hashlib
import json

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .ingest import IngestError, ingest
from .models import ApiToken, IdempotencyRecord, Tenant


def _error(status, message):
    return JsonResponse({"error": message}, status=status)


@csrf_exempt
@require_POST
def create_lead(request):
    auth = request.headers.get("Authorization", "")
    token = ApiToken.authenticate(auth[7:].strip() if auth.startswith("Bearer ") else "")
    if token is None:
        return _error(401, "token inválido")

    key = request.headers.get("Idempotency-Key", "").strip()
    if not key or len(key) > 255:
        return _error(400, "Idempotency-Key obrigatória (até 255 caracteres)")
    try:
        data = json.loads(request.body or b"{}")
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        return _error(400, "corpo precisa ser um objeto JSON")
    body_hash = hashlib.sha256(request.body).hexdigest()

    if token.is_service:
        # The app names the tenant (core uuid); it must be in the local copy.
        try:
            tenant = Tenant.objects.filter(uuid=str(data.get("tenant") or "")).first()
        except ValidationError:
            tenant = None
        if tenant is None:
            return _error(422, "tenant desconhecido (uuid do core)")
    else:
        tenant = token.tenant  # a tenant token never writes elsewhere
    if not tenant.is_active:
        return _error(403, "tenant inativo")

    previous = IdempotencyRecord.objects.filter(tenant=tenant, key=key).first()
    if previous:
        if previous.request_hash != body_hash:
            return _error(409, "Idempotency-Key já usada com outro corpo")
        return JsonResponse(previous.response, status=previous.status_code)

    try:
        with transaction.atomic():
            result = ingest(tenant, data, actor=f"api:{token.name}")
            answer = {
                "lead": str(result.lead.uuid),
                "contact": str(result.contact.uuid),
                "created": result.created,
                "duplicate": result.duplicate,
                "warnings": result.warnings,
            }
            status = 201 if result.created else 200
            IdempotencyRecord.objects.create(
                tenant=tenant, key=key, request_hash=body_hash, status_code=status, response=answer
            )
    except IngestError as exc:
        return _error(422, str(exc))
    except IntegrityError:
        # Same key raced in from a parallel retry: answer what it recorded.
        previous = IdempotencyRecord.objects.filter(tenant=tenant, key=key).first()
        if previous and previous.request_hash == body_hash:
            return JsonResponse(previous.response, status=previous.status_code)
        return _error(409, "requisição concorrente com a mesma Idempotency-Key")
    ApiToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
    return JsonResponse(answer, status=status)
