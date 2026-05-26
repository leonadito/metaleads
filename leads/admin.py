from django.contrib import admin
from .models import UserProfile, SheetMetadata, SyncLog


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'sheet_id', 'telegram_enabled', 'updated_at')
    list_filter = ('telegram_enabled',)
    search_fields = ('user__email',)


@admin.register(SheetMetadata)
class SheetMetadataAdmin(admin.ModelAdmin):
    list_display = ('user', 'sheet_id', 'last_sync')
    search_fields = ('user__email', 'sheet_id')


@admin.register(SyncLog)
class SyncLogAdmin(admin.ModelAdmin):
    list_display = ('user', 'sheet_name', 'lead_count', 'last_lead_row_index', 'synced_at')
    list_filter = ('sheet_name',)
    search_fields = ('user__email', 'sheet_name')
