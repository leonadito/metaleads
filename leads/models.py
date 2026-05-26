from django.db import models
from django.contrib.auth.models import User


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    sheet_id = models.CharField(max_length=255, blank=True, default='')
    telegram_chat_id = models.CharField(max_length=100, blank=True, default='')
    telegram_enabled = models.BooleanField(default=False)
    selected_tab_names = models.JSONField(default=list)  # [] = show all
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Profile({self.user.email})"

    @property
    def is_configured(self):
        return bool(self.sheet_id)


class SheetMetadata(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sheet_metadata')
    sheet_id = models.CharField(max_length=255)
    sheet_names = models.JSONField(default=list)  # list of {name, gid}
    last_sync = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('user', 'sheet_id')

    def __str__(self):
        return f"SheetMetadata({self.user.email}, {self.sheet_id})"


class SyncLog(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sync_logs')
    sheet_name = models.CharField(max_length=255)
    lead_count = models.IntegerField(default=0)
    last_lead_row_index = models.IntegerField(default=0)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('user', 'sheet_name')

    def __str__(self):
        return f"SyncLog({self.user.email}, {self.sheet_name})"
