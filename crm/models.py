"""CRM core of metaleads (Phase 4 of the Agitare ecosystem plan).

Design approved on 2026-10-14 (agitare_infra/docs/plano-ecossistema-agitare.md,
Fase 4): one Lead table for every source; what each ad platform adds lives in
LeadAttribution (one row per entry, with the platform's original payload);
Contact mirrors Chatwoot's contact; LeadEvent is the history.

Every row carries the tenant (copied from the Agitare Core) and every query
must filter by it.
"""
import hashlib
import secrets
import uuid

from django.db import models
from django.db.models import Q


class Tenant(models.Model):
    """Minimal copy of a client (tenant) of the Agitare Core, kept in sync by
    the core's tenant.* webhooks and a full daily sync. Never deleted here."""

    uuid = models.UUIDField("id no core", unique=True)
    name = models.CharField(max_length=200)
    slug = models.SlugField()
    whitelabel_uuid = models.UUIDField(null=True, blank=True)
    status = models.CharField(max_length=10, default="active")
    is_courtesy = models.BooleanField(default=False)
    chatwoot_account_id = models.PositiveIntegerField(null=True, blank=True)
    # Secret of the account webhook (signs Chatwoot's calls); read from the
    # core on demand (crm/chatwoot.py), never sent in the tenant webhooks.
    chatwoot_webhook_secret = models.CharField(max_length=128, blank=True)
    # Token of the account's Agent Bot "Agitare CRM" (labels, private notes,
    # the "Subetapa" attribute) and the administrator mentioned when a
    # conversation has nobody assigned; read from the core like the secret.
    chatwoot_bot_token = models.CharField(max_length=128, blank=True)
    chatwoot_admin_id = models.PositiveIntegerField(null=True, blank=True)
    chatwoot_admin_name = models.CharField(max_length=200, blank=True)
    waiting_alert_minutes = models.PositiveIntegerField(
        "tempo de aviso (min)", default=15,
        help_text="Mensagem do lead sem resposta de atendente por mais que isso gera aviso no Chatwoot.",
    )
    core_updated_at = models.DateTimeField(null=True, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def is_active(self):
        return self.status == "active"


class Contact(models.Model):
    """The person, inside one tenant (mirror of Chatwoot's contact)."""

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="contacts")
    name = models.CharField(max_length=255, blank=True)
    email = models.EmailField(blank=True)  # stored lower-case
    phone_e164 = models.CharField("telefone (E.164)", max_length=20, blank=True)
    country_code = models.CharField(max_length=2, blank=True)
    city = models.CharField(max_length=120, blank=True)
    company_name = models.CharField(max_length=200, blank=True)
    whatsapp_wa_id = models.CharField(max_length=40, blank=True)
    instagram_id = models.CharField(max_length=64, blank=True)
    messenger_psid = models.CharField(max_length=64, blank=True)
    chatwoot_contact_id = models.PositiveIntegerField(null=True, blank=True)
    custom_attributes = models.JSONField(default=dict, blank=True)
    consent_whatsapp = models.BooleanField(null=True, blank=True)
    consent_email = models.BooleanField(null=True, blank=True)
    consent_at = models.DateTimeField(null=True, blank=True)
    consent_source = models.CharField("origem do consentimento", max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "phone_e164"], condition=~Q(phone_e164=""), name="contact_unique_phone"),
            models.UniqueConstraint(fields=["tenant", "email"], condition=~Q(email=""), name="contact_unique_email"),
            models.UniqueConstraint(fields=["tenant", "whatsapp_wa_id"], condition=~Q(whatsapp_wa_id=""), name="contact_unique_wa"),
            models.UniqueConstraint(fields=["tenant", "instagram_id"], condition=~Q(instagram_id=""), name="contact_unique_ig"),
            models.UniqueConstraint(fields=["tenant", "messenger_psid"], condition=~Q(messenger_psid=""), name="contact_unique_psid"),
        ]

    def __str__(self):
        return self.name or self.phone_e164 or self.email or str(self.uuid)


class Stage(models.TextChoices):
    """The 6 fixed stages (sub-stages per tenant come in Phase 5)."""

    CRIADO = "criado", "Criado"
    EM_ANALISE = "em_analise", "Em análise"
    QUALIFICADO = "qualificado", "Qualificado"
    NAO_QUALIFICADO = "nao_qualificado", "Não qualificado"
    CONVERTIDO = "convertido", "Convertido"
    PERDIDO = "perdido", "Perdido"


CLOSED_STAGES = (Stage.CONVERTIDO, Stage.PERDIDO)


class Source(models.TextChoices):
    """Entry channel of the lead."""

    META_FORM = "meta_form", "Formulário Meta"
    WHATSAPP = "whatsapp", "WhatsApp"
    INSTAGRAM = "instagram", "Instagram Direct"
    MESSENGER = "messenger", "Messenger"
    AGIPAGES_FORM = "agipages_form", "Formulário do site (agipages)"
    GOOGLE_ADS_FORM = "google_ads_form", "Formulário Google Ads"
    EMAIL = "email", "E-mail"
    SITE_CHAT = "site_chat", "Chat do site"
    SHEET_IMPORT = "sheet_import", "Planilha importada"
    MANUAL = "manual", "Cadastro manual"
    API = "api", "API"


MAX_SUBSTAGES_PER_STAGE = 5


class Substage(models.Model):
    """Sub-stage of one of the 6 fixed stages, per tenant (Phase 5).

    Decision of 2026-10-14 (replaces "only people create sub-stages"): the
    LLM builds the funnel from the client's profile when the AI is turned on,
    and creates a sub-stage when a lead fits none, writing in the prompt why
    it exists. At most 5 active sub-stages per stage, for everyone. People
    may also create sub-stages and edit any prompt; a prompt can be locked
    against the LLM. Only people deactivate. Never deleted: leads and history
    keep pointing at it."""

    LLM, HUMAN = "llm", "human"
    AUTHORS = [(LLM, "IA"), (HUMAN, "Pessoa")]

    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="substages")
    stage = models.CharField("etapa", max_length=20, choices=Stage.choices)
    name = models.CharField("nome", max_length=100)
    order = models.PositiveIntegerField("ordem", default=0)
    prompt = models.TextField(
        "prompt", blank=True,
        help_text="Quando um lead deve estar nesta subetapa. Guia a LLM; obrigatório quando o tenant usa LLM.",
    )
    message_text = models.TextField(
        "mensagem ao entrar", blank=True,
        help_text="Enviada ao lead quando ele entra nesta subetapa (o envio chega na Fase 6).",
    )
    is_active = models.BooleanField("ativa", default=True)
    created_by = models.CharField("criada por", max_length=5, choices=AUTHORS, default=HUMAN)
    prompt_by = models.CharField("prompt escrito por", max_length=5, choices=AUTHORS, default=HUMAN)
    prompt_locked = models.BooleanField(
        "bloquear edição do prompt pela IA", default=False,
        help_text="Ligado: a IA não altera este prompt (só uma pessoa).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "subetapa"
        ordering = ["order", "id"]
        constraints = [models.UniqueConstraint(fields=["tenant", "stage", "name"], name="substage_unique_name")]

    def __str__(self):
        return f"{self.get_stage_display()} > {self.name}"


class Lead(models.Model):
    """The opportunity. One table for every source; one open lead per contact
    (a new entry of the same contact while open becomes a history event)."""

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="leads")
    contact = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name="leads")
    source = models.CharField("canal de entrada", max_length=20, choices=Source.choices)
    stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.CRIADO, db_index=True)
    substage = models.ForeignKey(Substage, on_delete=models.PROTECT, null=True, blank=True, related_name="leads")
    # Automatic moves stopped until a person decides (back-and-forth lock,
    # LLM unsure...): the lead waits in the human review.
    frozen_at = models.DateTimeField("em revisão humana desde", null=True, blank=True, db_index=True)
    review_reason = models.TextField("motivo da revisão", blank=True)
    # Last message of the lead still without an agent's answer (Chatwoot).
    waiting_since = models.DateTimeField("esperando resposta desde", null=True, blank=True, db_index=True)
    waiting_alerted_at = models.DateTimeField(null=True, blank=True)
    external_id = models.CharField("id na fonte", max_length=128, blank=True)
    form_answers = models.JSONField("respostas do formulário", default=dict, blank=True)
    landing_page_uuid = models.UUIDField(null=True, blank=True)
    landing_page_name = models.CharField(max_length=255, blank=True)
    landing_page_url = models.URLField(max_length=500, blank=True)
    conversation_id = models.PositiveIntegerField("conversa no Chatwoot", null=True, blank=True)
    summary = models.TextField("resumo da IA", blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "source", "external_id"], condition=~Q(external_id=""), name="lead_unique_source_id"
            ),
            models.UniqueConstraint(
                fields=["contact"], condition=~Q(stage__in=CLOSED_STAGES), name="lead_one_open_per_contact"
            ),
        ]

    def __str__(self):
        return f"{self.contact} ({self.get_stage_display()})"

    @property
    def is_open(self):
        return self.stage not in CLOSED_STAGES


class Platform(models.TextChoices):
    META = "meta", "Meta"
    GOOGLE = "google", "Google"
    ORGANIC = "organic", "Orgânico"
    DIRECT = "direct", "Direto"
    OTHER = "other", "Outro"


class AttributionKind(models.TextChoices):
    FORM = "form", "Formulário"
    CTWA = "ctwa", "Click-to-WhatsApp"
    LANDING_PAGE = "landing_page", "Página"
    OTHER = "other", "Outro"


class LeadAttribution(models.Model):
    """Where one entry of the lead came from. One row per entry, so the first
    and the latest campaign are both kept; `raw` holds the platform's original
    payload (nothing is lost, and it can be reprocessed)."""

    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="attributions")
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="+")
    occurred_at = models.DateTimeField(db_index=True)
    platform = models.CharField(max_length=10, choices=Platform.choices, default=Platform.DIRECT)
    kind = models.CharField(max_length=15, choices=AttributionKind.choices, default=AttributionKind.OTHER)
    campaign_id = models.CharField(max_length=64, blank=True, db_index=True)
    campaign_name = models.CharField(max_length=255, blank=True)
    adset_id = models.CharField("conjunto / grupo de anúncios", max_length=64, blank=True)
    adset_name = models.CharField(max_length=255, blank=True)
    ad_id = models.CharField(max_length=64, blank=True, db_index=True)
    ad_name = models.CharField(max_length=255, blank=True)
    form_id = models.CharField(max_length=64, blank=True)
    form_name = models.CharField(max_length=255, blank=True)
    placement = models.CharField("posicionamento", max_length=20, blank=True)  # facebook, instagram, whatsapp...
    is_organic = models.BooleanField(null=True, blank=True)
    utm_source = models.CharField(max_length=255, blank=True)
    utm_medium = models.CharField(max_length=255, blank=True)
    utm_campaign = models.CharField(max_length=255, blank=True)
    utm_term = models.CharField(max_length=255, blank=True)
    utm_content = models.CharField(max_length=255, blank=True)
    fbclid = models.CharField(max_length=255, blank=True)
    gclid = models.CharField(max_length=255, blank=True)
    ctwa_clid = models.CharField(max_length=255, blank=True)
    creative_headline = models.CharField(max_length=500, blank=True)
    creative_body = models.TextField(blank=True)
    creative_source_url = models.URLField(max_length=500, blank=True)
    creative_media_url = models.URLField(max_length=1000, blank=True)
    raw = models.JSONField("pacote original", default=dict, blank=True)

    class Meta:
        ordering = ["occurred_at"]


class EventType(models.TextChoices):
    CREATED = "created", "Entrada"
    REENTRY = "reentry", "Nova entrada"
    STAGE_CHANGED = "stage_changed", "Mudança de etapa"
    MESSAGE = "message", "Mensagem"
    EMAIL_OPENED = "email_opened", "E-mail aberto"
    EMAIL_CLICKED = "email_clicked", "Clique no e-mail"
    NOTE = "note", "Nota"
    REVIEW = "review", "Revisão humana"
    RELEASED = "released", "Liberado da revisão"


class LeadEvent(models.Model):
    """History of the lead. Messages keep only the Chatwoot message id, never
    the text (conversations live only in Chatwoot, decision of 2026-10-12)."""

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="events")
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="+")
    type = models.CharField(max_length=20, choices=EventType.choices)
    actor = models.CharField("quem fez", max_length=200, blank=True)  # "sistema", "api:<token>", pessoa, "llm"
    reason = models.TextField("motivo", blank=True)
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at"]


def _hash_token(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


class ApiToken(models.Model):
    """Token of the lead intake API, one or more per tenant. Only the hash is
    stored; the value is shown once, when created."""

    tenant = models.ForeignKey(
        Tenant, on_delete=models.PROTECT, related_name="api_tokens", null=True, blank=True
    )
    # Service token (Agitare's own apps, e.g. agipages, which serves every
    # tenant): no fixed tenant; each request names the tenant by its core
    # uuid, which must exist in the local copy. Tenant tokens are for
    # outside integrations.
    is_service = models.BooleanField("token de serviço", default=False)
    name = models.CharField(max_length=100)
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    prefix = models.CharField(max_length=12, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{'serviço' if self.is_service else self.tenant} · {self.name} ({self.prefix}…)"

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(is_service=True, tenant__isnull=True) | Q(is_service=False, tenant__isnull=False),
                name="api_token_tenant_or_service",
            )
        ]

    @classmethod
    def issue(cls, tenant, name, service=False):
        raw = "ml_" + secrets.token_urlsafe(32)
        token = cls.objects.create(
            tenant=None if service else tenant, is_service=service, name=name,
            token_hash=_hash_token(raw), prefix=raw[:10],
        )
        return token, raw

    @classmethod
    def authenticate(cls, raw):
        return (
            cls.objects.select_related("tenant")
            .filter(token_hash=_hash_token(raw or ""), revoked_at__isnull=True)
            .first()
        )


class IdempotencyRecord(models.Model):
    """Answer already given for an Idempotency-Key, so a retried request gets
    the same answer instead of a second lead."""

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="+")
    key = models.CharField(max_length=255)
    request_hash = models.CharField(max_length=64)
    status_code = models.PositiveSmallIntegerField()
    response = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "key"], name="idempotency_unique_key")]


class CoreWebhookReceipt(models.Model):
    """Ids of core events already applied (the core may deliver twice)."""

    event_id = models.UUIDField(unique=True)
    event_type = models.CharField(max_length=50)
    received_at = models.DateTimeField(auto_now_add=True)


class MetaPage(models.Model):
    """A Facebook page whose Lead Ads forms feed a tenant (Meta Lead Ads
    straight from the API; the Google Sheet is no longer the store). Access
    goes through Agitare's Meta app and system user (token in the .env, never
    here); this row only holds ids and the reading state."""

    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="meta_pages")
    page_id = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=255, blank=True)
    dataset_id = models.CharField(
        "conjunto de dados (pixel)", max_length=32, blank=True,
        help_text="Recebe as etapas do funil (API de Conversões para CRM).",
    )
    is_active = models.BooleanField("ativa", default=True)
    send_events = models.BooleanField(
        "envia etapas à Meta", default=False,
        help_text="Ligar só depois de desligar a integração da planilha na Meta (senão o funil recebe em dobro).",
    )
    form_names = models.JSONField("nomes dos formulários", default=dict, blank=True)
    subscribed_at = models.DateTimeField("inscrita no leadgen em", null=True, blank=True)
    polled_until = models.DateTimeField("lida até", null=True, blank=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "página da Meta"
        verbose_name_plural = "páginas da Meta"

    def __str__(self):
        return self.name or self.page_id


class MetaCrmEvent(models.Model):
    """Outbox of funnel stages sent to Meta (Conversions API for CRM, the same
    funnel the Google Sheet integration fed). One row per stage change per
    Meta lead id; Meta wants the full history, not a snapshot, and refuses
    events older than 7 days."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    EXPIRED = "expired"
    STATUS_CHOICES = [(PENDING, "Pendente"), (SENT, "Enviado"), (FAILED, "Recusado"), (EXPIRED, "Expirado")]

    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="meta_events")
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="+")
    dataset_id = models.CharField(max_length=32)
    meta_lead_id = models.CharField(max_length=32)
    event_name = models.CharField(max_length=50)
    event_time = models.DateTimeField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "etapa enviada à Meta"
        verbose_name_plural = "etapas enviadas à Meta"
        ordering = ["event_time", "id"]


class GoogleLeadFormKey(models.Model):
    """Key of a Google Ads lead form webhook (the "google_key" set in the
    form). It names the tenant; only the hash is stored, the value is shown
    once in the admin, when created, to be pasted in Google Ads."""

    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="google_form_keys")
    name = models.CharField("formulário / uso", max_length=100)
    key_hash = models.CharField(max_length=64, unique=True, editable=False)
    prefix = models.CharField(max_length=12, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_lead_at = models.DateTimeField("último lead em", null=True, blank=True)
    last_test_at = models.DateTimeField("último teste do Google em", null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "chave de formulário do Google Ads"
        verbose_name_plural = "chaves de formulário do Google Ads"

    def __str__(self):
        return f"{self.tenant} · {self.name} ({self.prefix}…)"

    @classmethod
    def new_key(cls):
        return "gk_" + secrets.token_urlsafe(24)

    def set_key(self, raw):
        self.key_hash = _hash_token(raw)
        self.prefix = raw[:10]

    @classmethod
    def authenticate(cls, raw):
        if not raw:
            return None
        return (
            cls.objects.select_related("tenant")
            .filter(key_hash=_hash_token(raw), revoked_at__isnull=True)
            .first()
        )


class OrphanReport(models.Model):
    """Result of the nightly orphan check (crm/orphans.py): references to
    things that no longer exist or are inactive in another app. Report and
    alert only; nothing is ever fixed automatically."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    findings = models.JSONField("achados", default=list)
    count = models.PositiveIntegerField("quantidade", default=0)
    errors = models.JSONField("checagens que falharam", default=list)

    class Meta:
        verbose_name = "relatório de órfãos"
        verbose_name_plural = "relatórios de órfãos"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.created_at:%d/%m/%Y %H:%M} · {self.count} achado(s)"
