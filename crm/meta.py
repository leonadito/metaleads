"""Meta Lead Ads straight from the API, and the funnel back to Meta (Phase 4,
approved on 2026-10-14). Replaces the Google Sheet as the store of the leads.

In (Meta → metaleads):
- Agitare's Meta app is subscribed to `leadgen` on each connected page
  (MetaPage) and Meta POSTs here, signed with the app secret:
      X-Hub-Signature-256: sha256=<hex HMAC-SHA256(app_secret, body)>
  The GET with hub.challenge is Meta's verification of the URL.
- The webhook only carries ids: a task fetches the lead from the Graph API
  (the lead node already brings ad/adset/campaign names) and records it
  through crm.ingest; leadgen_id = Lead.external_id, so it never duplicates.
- Safety net: every hour each page's forms are read for leads created since
  the last reading (a webhook can be lost). Meta only keeps leads downloadable
  for 90 days, so everything that arrives is stored: the standard fields go
  to the Contact, the custom questions to Lead.form_answers and the whole
  Graph API answer to LeadAttribution.raw.

Out (metaleads → Meta, Conversions API for CRM, the funnel the sheet fed):
- every stage of a Meta form lead becomes a MetaCrmEvent row (entry =
  "Criado"), only on pages with send_events on; a task sends them in batches
  to POST /{dataset_id}/events with the lead_id and nothing personal.
- event names are exactly the texts the sheet wrote in lead_status, so the
  funnel already set up in each client's Events Manager keeps working.
"""
import hashlib
import hmac
import json
import logging
from datetime import timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django_tasks import task
from django_tasks_db.models import DBTaskResult

from .ingest import IngestError, ingest
from .models import EventType, LeadAttribution, LeadEvent, MetaCrmEvent, MetaPage, Source, Stage

logger = logging.getLogger(__name__)

LEAD_FIELDS = (
    "id,created_time,field_data,ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,"
    "form_id,is_organic,platform,custom_disclaimer_responses"
)
# Same texts the sheet wrote in lead_status (leads/views.py SHEET_WRITE_VALUES):
# the funnel configured in Events Manager is keyed by these names.
EVENT_NAMES = {
    Stage.CRIADO: "Criado",
    Stage.EM_ANALISE: "Em análise",
    Stage.QUALIFICADO: "Qualificado",
    Stage.NAO_QUALIFICADO: "Não qualificado",
    Stage.CONVERTIDO: "Convertido",
    Stage.PERDIDO: "Perdido",
}
PLACEMENTS = {"fb": "facebook", "ig": "instagram", "msg": "messenger", "wa": "whatsapp"}
CONTACT_FIELDS = {"full_name", "first_name", "last_name", "email", "phone_number", "city", "company_name", "country"}
MAX_EVENT_AGE = timedelta(days=7) - timedelta(hours=1)  # Meta refuses older than 7 days
BATCH_SIZE = 500  # Meta takes up to 1,000; one bad event discards the whole batch
SEND_BACKOFF = [timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2), timedelta(hours=6)]
POLL_EVERY = timedelta(hours=1)
POLL_OVERLAP = timedelta(minutes=10)
BACKFILL = timedelta(days=90)
POLL_PATH = "crm.meta.poll_pages"
SEND_PATH = "crm.meta.send_events"


class MetaError(Exception):
    def __init__(self, message, status=None, permanent=False):
        super().__init__(message)
        self.status = status
        self.permanent = permanent


# ── Graph API ────────────────────────────────────────────────────────────────

def graph(method, path, token=None, params=None, data=None):
    url = path if path.startswith("https://") else f"https://graph.facebook.com/{settings.META_GRAPH_VERSION}/{path.lstrip('/')}"
    params = dict(params or {})
    if not path.startswith("https://"):  # paging URLs already carry the token
        params["access_token"] = token or settings.META_ACCESS_TOKEN
    response = requests.request(method, url, params=params, data=data, timeout=20)
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not 200 <= response.status_code < 300:
        error = body.get("error") or {}
        raise MetaError(
            f"{method} {path.split('?')[0]}: HTTP {response.status_code} {error.get('message') or response.text[:300]}",
            status=response.status_code,
            permanent=400 <= response.status_code < 500 and response.status_code != 429 and error.get("code") not in (4, 17, 32, 613),
        )
    return body


def page_token(page_id):
    """Page access token, derived from the system user's token."""
    return graph("GET", page_id, params={"fields": "access_token,name"})


def subscribe_page(page):
    """Subscribes Agitare's app to the page's `leadgen` field."""
    info = page_token(page.page_id)
    graph("POST", f"{page.page_id}/subscribed_apps", token=info["access_token"], params={"subscribed_fields": "leadgen"})
    MetaPage.objects.filter(pk=page.pk).update(subscribed_at=timezone.now(), name=info.get("name") or page.name)


# ── Webhook ──────────────────────────────────────────────────────────────────

def _valid_signature(body, signature):
    secret = settings.META_APP_SECRET
    if not (secret and signature.startswith("sha256=")):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature[7:], expected)


@csrf_exempt
def meta_webhook(request):
    if request.method == "GET":  # Meta checking the callback URL
        if (
            request.GET.get("hub.mode") == "subscribe"
            and settings.META_VERIFY_TOKEN
            and hmac.compare_digest(request.GET.get("hub.verify_token", ""), settings.META_VERIFY_TOKEN)
        ):
            return HttpResponse(request.GET.get("hub.challenge", ""), content_type="text/plain")
        return HttpResponseForbidden("token de verificação inválido")
    if request.method != "POST":
        return HttpResponse(status=405)
    if not _valid_signature(request.body, request.headers.get("X-Hub-Signature-256", "")):
        return HttpResponseForbidden("assinatura inválida")
    try:
        payload = json.loads(request.body)
    except ValueError:
        return HttpResponseBadRequest("evento malformado")
    if payload.get("object") != "page":
        return HttpResponse(status=200)
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            if change.get("field") != "leadgen" or not value.get("leadgen_id"):
                continue
            page_id = str(value.get("page_id") or entry.get("id") or "")
            page = MetaPage.objects.filter(page_id=page_id, is_active=True).first()
            if page is None:
                # 200 anyway: Meta would keep retrying and could disable the app's webhook.
                logger.warning("Lead %s da página %s, que não está conectada", value["leadgen_id"], page_id)
                continue
            fetch_lead.enqueue(page.pk, str(value["leadgen_id"]), value)
    return HttpResponse(status=200)


# ── Lead → ingest ────────────────────────────────────────────────────────────

def _answers(field_data):
    out = {}
    for item in field_data or []:
        values = item.get("values") or []
        out[item.get("name") or ""] = values[0] if len(values) == 1 else values
    return out


def lead_to_ingest(page, lead, webhook_value=None):
    """Graph API lead node → body of crm.ingest."""
    answers = _answers(lead.get("field_data"))
    name = answers.get("full_name") or " ".join(
        str(answers[k]) for k in ("first_name", "last_name") if answers.get(k)
    )
    country = str(answers.get("country") or "")
    form_id = str(lead.get("form_id") or (webhook_value or {}).get("form_id") or "")
    return {
        "source": Source.META_FORM,
        "external_id": str(lead["id"]),
        "occurred_at": lead.get("created_time"),
        "name": str(name),
        "email": str(answers.get("email") or ""),
        "phone": str(answers.get("phone_number") or ""),
        "city": str(answers.get("city") or ""),
        "company": str(answers.get("company_name") or ""),
        "country_code": country if len(country) == 2 else "",
        "form_answers": {k: v for k, v in answers.items() if k not in CONTACT_FIELDS},
        "attribution": {
            "platform": "meta",
            "kind": "form",
            "campaign_id": lead.get("campaign_id", ""),
            "campaign_name": lead.get("campaign_name", ""),
            "adset_id": lead.get("adset_id", ""),
            "adset_name": lead.get("adset_name", ""),
            "ad_id": lead.get("ad_id", ""),
            "ad_name": lead.get("ad_name", ""),
            "form_id": form_id,
            "form_name": page.form_names.get(form_id, ""),
            "placement": PLACEMENTS.get(lead.get("platform"), lead.get("platform") or ""),
            "is_organic": lead.get("is_organic"),
            "raw": {"page_id": page.page_id, "lead": lead, "webhook": webhook_value or {}},
        },
    }


def _form_name(page, form_id):
    if not form_id or form_id in page.form_names:
        return
    try:
        name = graph("GET", form_id, params={"fields": "name"}).get("name") or ""
    except (MetaError, requests.RequestException):
        return  # the name is a nicety; the lead is not held back for it
    page.form_names = {**page.form_names, form_id: name}
    MetaPage.objects.filter(pk=page.pk).update(form_names=page.form_names)


def store_lead(page, lead, webhook_value=None):
    """Records one Meta lead (from the webhook fetch or the hourly reading)."""
    _form_name(page, str(lead.get("form_id") or ""))
    data = lead_to_ingest(page, lead, webhook_value)
    try:
        with transaction.atomic():
            result = ingest(page.tenant, data, actor="meta")
            if not result.duplicate:
                at = parse_datetime(data["occurred_at"] or "") if not result.created else result.lead.created_at
                record_entry(result.lead, page, data["external_id"], at)
    except IngestError as exc:
        logger.warning("Lead %s da Meta não entrou: %s", data["external_id"], exc)
        return None
    return result


@task
def fetch_lead(page_pk, leadgen_id, webhook_value=None):
    page = MetaPage.objects.select_related("tenant").filter(pk=page_pk, is_active=True).first()
    if page is None:
        return "página desconectada"
    try:
        token = page_token(page.page_id)["access_token"]
        lead = graph("GET", leadgen_id, token=token, params={"fields": LEAD_FIELDS})
    except MetaError as exc:
        MetaPage.objects.filter(pk=page.pk).update(last_error=str(exc)[:2000])
        if exc.permanent:  # the hourly reading tries again anyway
            logger.warning("Lead %s não pôde ser lido: %s", leadgen_id, exc)
            return f"erro: {exc}"
        raise
    result = store_lead(page, lead, webhook_value)
    return str(result.lead.uuid) if result else "recusado"


# ── Hourly reading (safety net) and backfill ─────────────────────────────────

def _paged(path, params, token=None):
    body = graph("GET", path, token=token, params=params)
    while True:
        yield from body.get("data") or []
        next_url = (body.get("paging") or {}).get("next")
        if not next_url:
            return
        body = graph("GET", next_url)


def poll_page(page, since=None):
    """Reads every form of the page for leads created after `since` (default:
    last reading minus an overlap; first reading: 90 days). Returns how many
    new leads were recorded."""
    started = timezone.now()
    if since is None:
        since = (page.polled_until - POLL_OVERLAP) if page.polled_until else started - BACKFILL
    token = page_token(page.page_id)["access_token"]
    forms = list(_paged(f"{page.page_id}/leadgen_forms", {"fields": "id,name", "limit": 100}, token))
    names = {**page.form_names, **{f["id"]: f.get("name") or "" for f in forms}}
    if names != page.form_names:
        page.form_names = names
        MetaPage.objects.filter(pk=page.pk).update(form_names=names)
    new = 0
    filtering = json.dumps([{"field": "time_created", "operator": "GREATER_THAN", "value": int(since.timestamp())}])
    for form in forms:
        for lead in _paged(f"{form['id']}/leads", {"fields": LEAD_FIELDS, "filtering": filtering, "limit": 100}, token):
            lead.setdefault("form_id", form["id"])
            result = store_lead(page, lead)
            new += bool(result and not result.duplicate)
    MetaPage.objects.filter(pk=page.pk).update(polled_until=started, last_error="")
    return new


@task
def poll_pages():
    """Every hour: reads all active pages, then sends pending funnel events
    (retries included). Re-enqueues itself."""
    try:
        if settings.META_ACCESS_TOKEN:
            for page in MetaPage.objects.select_related("tenant").filter(is_active=True):
                try:
                    count = poll_page(page)
                    if count:
                        logger.info("Página %s: %s lead(s) que o webhook não trouxe", page, count)
                except (MetaError, requests.RequestException) as exc:
                    MetaPage.objects.filter(pk=page.pk).update(last_error=str(exc)[:2000])
                    logger.warning("Leitura da página %s falhou: %s", page, exc)
        flush_events()
    finally:
        poll_pages.using(run_after=timezone.now() + POLL_EVERY).enqueue()


@task
def poll_one(page_pk):
    """Reads one page now (admin action; the first reading imports 90 days)."""
    page = MetaPage.objects.select_related("tenant").get(pk=page_pk)
    try:
        return poll_page(page)
    except (MetaError, requests.RequestException) as exc:
        MetaPage.objects.filter(pk=page.pk).update(last_error=str(exc)[:2000])
        raise


def ensure_hourly_poll():
    if DBTaskResult.objects.filter(task_path=POLL_PATH, status__in=["READY", "RUNNING"]).exists():
        return False
    poll_pages.enqueue()
    return True


# ── Funnel back to Meta (Conversions API for CRM) ────────────────────────────

def meta_lead_ids(lead):
    """Meta lead ids of this CRM lead: the one that opened it and any form
    re-entry (both are leads Meta knows)."""
    ids = []
    if lead.source == Source.META_FORM and lead.external_id:
        ids.append(lead.external_id)
    for external_id in LeadEvent.objects.filter(
        lead=lead, type=EventType.REENTRY, data__source=Source.META_FORM
    ).values_list("data__external_id", flat=True):
        external_id = str(external_id or "")  # SQLite gives digits back as a number
        if external_id and external_id not in ids:
            ids.append(external_id)
    return ids


def _page_for(lead, meta_lead_id):
    attribution = (
        LeadAttribution.objects.filter(lead=lead, raw__lead__id=meta_lead_id).only("raw").first()
        or LeadAttribution.objects.filter(lead=lead, raw__has_key="page_id").only("raw").first()
    )
    if attribution is None:
        return None
    return MetaPage.objects.filter(page_id=attribution.raw.get("page_id")).first()


def _queue(lead, page, meta_lead_id, stage, at):
    if not (page and page.send_events and page.dataset_id and stage in EVENT_NAMES):
        return False
    MetaCrmEvent.objects.create(
        lead=lead, tenant=lead.tenant, dataset_id=page.dataset_id, meta_lead_id=meta_lead_id,
        event_name=EVENT_NAMES[stage], event_time=at,
    )
    transaction.on_commit(schedule_send)
    return True


def record_entry(lead, page, meta_lead_id, at=None):
    """A Meta form lead entered (new lead or re-entry): Meta wants the first
    stage too. The entry is "Criado" even when the lead was already further
    along, because that Meta lead is new."""
    return _queue(lead, page, meta_lead_id, Stage.CRIADO, at or timezone.now())


def record_stage(lead, stage, at=None):
    """A stage change of the CRM lead, for every Meta lead id it has."""
    at = at or timezone.now()
    return sum(_queue(lead, _page_for(lead, mid), mid, stage, at) for mid in meta_lead_ids(lead))


def schedule_send():
    if DBTaskResult.objects.filter(task_path=SEND_PATH, status="READY").exists():
        return
    send_events.enqueue()


def _payload(event):
    return {
        "event_name": event.event_name,
        "event_time": int(event.event_time.timestamp()),
        "action_source": "system_generated",
        "user_data": {"lead_id": int(event.meta_lead_id)},
        "custom_data": {"event_source": "crm", "lead_event_source": settings.META_LEAD_EVENT_SOURCE},
    }


def _post(dataset_id, events):
    data = {"data": json.dumps([_payload(e) for e in events])}
    if settings.META_TEST_EVENT_CODE:
        data["test_event_code"] = settings.META_TEST_EVENT_CODE
    return graph("POST", f"{dataset_id}/events", data=data)


def _failed(events, exc, now):
    for event in events:
        event.attempts += 1
        event.last_error = str(exc)[:2000]
        if exc.permanent or event.attempts > len(SEND_BACKOFF):
            event.status = MetaCrmEvent.FAILED
            event.next_attempt_at = None
        else:
            event.next_attempt_at = now + SEND_BACKOFF[event.attempts - 1]
        event.save(update_fields=["attempts", "last_error", "status", "next_attempt_at"])


def _sent(events, now):
    MetaCrmEvent.objects.filter(pk__in=[e.pk for e in events]).update(
        status=MetaCrmEvent.SENT, sent_at=now, last_error="", next_attempt_at=None
    )


def flush_events():
    """Sends what is due. Returns (sent, failed, expired)."""
    if not (settings.META_CAPI_ENABLED and settings.META_ACCESS_TOKEN):
        return (0, 0, 0)
    now = timezone.now()
    due = MetaCrmEvent.objects.filter(status=MetaCrmEvent.PENDING).exclude(next_attempt_at__gt=now)
    expired = due.filter(event_time__lt=now - MAX_EVENT_AGE).update(
        status=MetaCrmEvent.EXPIRED, last_error="mais de 7 dias: a Meta não aceita mais"
    )
    sent = failed = 0
    datasets = due.values_list("dataset_id", flat=True).distinct()
    for dataset_id in list(datasets):
        while events := list(due.filter(dataset_id=dataset_id)[:BATCH_SIZE]):
            s_, f_ = _flush_batch(dataset_id, events, now)
            sent, failed = sent + s_, failed + f_
    return (sent, failed, expired)


def _flush_batch(dataset_id, events, now):
    """Returns (sent, failed); every event leaves the due list (sent, failed,
    or with a later next_attempt_at)."""
    try:
        _post(dataset_id, events)
        _sent(events, now)
        return (len(events), 0)
    except requests.RequestException as exc:
        _failed(events, MetaError(str(exc)), now)
        return (0, len(events))
    except MetaError as exc:
        if not exc.permanent or len(events) == 1:
            _failed(events, exc, now)
            return (0, len(events))
    # One bad event discards the whole batch: send one by one to find it.
    sent = failed = 0
    for event in events:
        try:
            _post(dataset_id, [event])
            _sent([event], now)
            sent += 1
        except requests.RequestException as exc:
            _failed([event], MetaError(str(exc)), now)
            failed += 1
        except MetaError as exc:
            _failed([event], exc, now)
            failed += 1
    return (sent, failed)


@task
def send_events():
    sent, failed, expired = flush_events()
    retry = (
        MetaCrmEvent.objects.filter(status=MetaCrmEvent.PENDING, next_attempt_at__isnull=False)
        .order_by("next_attempt_at").values_list("next_attempt_at", flat=True).first()
    )
    if retry and settings.META_CAPI_ENABLED:
        send_events.using(run_after=retry).enqueue()
    return {"enviados": sent, "recusados": failed, "expirados": expired}
