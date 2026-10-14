from django.urls import path

from . import api, core_sync

urlpatterns = [
    path("api/v1/leads", api.create_lead, name="api_create_lead"),
    path("webhooks/core/", core_sync.core_webhook, name="core_webhook"),
]
