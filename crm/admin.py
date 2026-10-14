from django.contrib import admin
from django.utils import timezone

from .models import ApiToken, Contact, Lead, LeadAttribution, LeadEvent, Tenant


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "status", "is_courtesy", "chatwoot_account_id", "synced_at"]
    list_filter = ["status", "is_courtesy"]
    search_fields = ["name", "slug"]
    readonly_fields = [f.name for f in Tenant._meta.fields]

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
