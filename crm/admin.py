from django.contrib import admin
from django.utils import timezone

from . import meta
from .funnel import change_stage
from .models import (
    ApiToken, Contact, GoogleLeadFormKey, Lead, LeadAttribution, LeadEvent, MetaCrmEvent, MetaPage, OrphanReport, Tenant,
)


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "status", "is_courtesy", "chatwoot_account_id", "synced_at"]
    list_filter = ["status", "is_courtesy"]
    search_fields = ["name", "slug"]
    exclude = ["chatwoot_webhook_secret", "chatwoot_bot_token"]
    readonly_fields = [f.name for f in Tenant._meta.fields if f.name not in ("chatwoot_webhook_secret", "chatwoot_bot_token")]

    def has_add_permission(self, request):
        return False  # copied from the core

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ["__str__", "tenant", "phone_e164", "email", "city", "created_at"]
    list_filter = ["tenant"]
    search_fields = ["name", "phone_e164", "email"]
    readonly_fields = ["uuid", "created_at", "updated_at"]


class AttributionInline(admin.TabularInline):
    model = LeadAttribution
    extra = 0
    fields = ["occurred_at", "platform", "kind", "campaign_name", "adset_name", "ad_name", "form_name", "utm_source", "utm_campaign"]
    readonly_fields = fields
    can_delete = False


class EventInline(admin.TabularInline):
    model = LeadEvent
    extra = 0
    fields = ["created_at", "type", "actor", "reason"]
    readonly_fields = fields
    can_delete = False


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ["contact", "tenant", "source", "stage", "created_at"]
    list_filter = ["tenant", "source", "stage"]
    search_fields = ["contact__name", "contact__phone_e164", "contact__email", "external_id"]
    readonly_fields = ["uuid", "created_at", "updated_at"]
    inlines = [AttributionInline, EventInline]

    def save_model(self, request, obj, form, change):
        # The stage goes through change_stage (history + Meta funnel).
        new_stage = obj.stage
        if change and "stage" in form.changed_data:
            obj.stage = form.initial["stage"]
        super().save_model(request, obj, form, change)
        if change and "stage" in form.changed_data:
            change_stage(obj, new_stage, actor=request.user.get_username(), reason="alterado no admin")
            obj.refresh_from_db()


@admin.register(ApiToken)
class ApiTokenAdmin(admin.ModelAdmin):
    list_display = ["name", "tenant", "prefix", "created_at", "last_used_at", "revoked_at"]
    list_filter = ["tenant"]
    readonly_fields = ["tenant", "name", "prefix", "created_at", "last_used_at", "revoked_at"]
    exclude = ["token_hash"]
    actions = ["revoke"]

    def has_add_permission(self, request):
        return False  # created by the create_api_token command (value shown only there)

    @admin.action(description="Revogar")
    def revoke(self, request, queryset):
        queryset.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())


@admin.register(MetaPage)
class MetaPageAdmin(admin.ModelAdmin):
    list_display = ["__str__", "page_id", "tenant", "dataset_id", "is_active", "send_events", "subscribed_at", "polled_until"]
    list_filter = ["is_active", "send_events"]
    search_fields = ["name", "page_id"]
    fields = ["tenant", "page_id", "name", "dataset_id", "is_active", "send_events",
              "subscribed_at", "polled_until", "form_names", "last_error"]
    readonly_fields = ["subscribed_at", "polled_until", "form_names", "last_error"]
    actions = ["subscribe", "read_now"]

    @admin.action(description="Inscrever o app no leadgen da página")
    def subscribe(self, request, queryset):
        for page in queryset:
            try:
                meta.subscribe_page(page)
                self.message_user(request, f"{page}: inscrita.")
            except (meta.MetaError, meta.requests.RequestException) as exc:
                self.message_user(request, f"{page}: {exc}", level="error")

    @admin.action(description="Ler os formulários agora (a primeira leitura traz 90 dias)")
    def read_now(self, request, queryset):
        for page in queryset:
            meta.poll_one.enqueue(page.pk)
        self.message_user(request, f"{queryset.count()} leitura(s) na fila.")


@admin.register(MetaCrmEvent)
class MetaCrmEventAdmin(admin.ModelAdmin):
    list_display = ["event_name", "meta_lead_id", "tenant", "event_time", "status", "attempts", "sent_at"]
    list_filter = ["status", "tenant", "event_name"]
    search_fields = ["meta_lead_id"]

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in MetaCrmEvent._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(GoogleLeadFormKey)
class GoogleLeadFormKeyAdmin(admin.ModelAdmin):
    list_display = ["name", "tenant", "prefix", "created_at", "last_test_at", "last_lead_at", "revoked_at"]
    list_filter = ["tenant"]
    actions = ["revoke"]

    def get_fields(self, request, obj=None):
        if obj is None:
            return ["tenant", "name"]
        return ["tenant", "name", "prefix", "created_at", "last_test_at", "last_lead_at", "revoked_at"]

    def get_readonly_fields(self, request, obj=None):
        return [] if obj is None else ["tenant", "prefix", "created_at", "last_test_at", "last_lead_at", "revoked_at"]

    def save_model(self, request, obj, form, change):
        if not change:
            raw = GoogleLeadFormKey.new_key()
            obj.set_key(raw)
            super().save_model(request, obj, form, change)
            # Shown once, only here: paste it in the Google Ads lead form.
            self.message_user(
                request,
                f"Chave criada. Copie agora, ela não aparece de novo. No formulário do Google Ads: "
                f"URL {request.build_absolute_uri('/webhooks/google-ads/')} e chave {raw}",
                level="warning",
            )
            return
        super().save_model(request, obj, form, change)

    @admin.action(description="Revogar")
    def revoke(self, request, queryset):
        queryset.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())


@admin.register(OrphanReport)
class OrphanReportAdmin(admin.ModelAdmin):
    list_display = ["created_at", "count", "failed_checks"]
    fields = ["created_at", "count", "errors", "findings_table"]
    readonly_fields = fields
    actions = ["run_now"]

    @admin.display(description="checagens que falharam")
    def failed_checks(self, obj):
        return len(obj.errors)

    @admin.display(description="achados")
    def findings_table(self, obj):
        from django.utils.html import format_html_join

        return format_html_join(
            "", "<div>{} · {} {}</div>",
            ((f["message"], f.get("tenant") or "-", f.get("ref") or "") for f in obj.findings),
        ) or "nenhum"

    def has_add_permission(self, request):
        return False

    @admin.action(description="Rodar a checagem agora")
    def run_now(self, request, queryset):
        from .orphans import run_check

        report = run_check()
        self.message_user(request, f"Checagem feita: {report.count} achado(s), {len(report.errors)} falha(s).")
