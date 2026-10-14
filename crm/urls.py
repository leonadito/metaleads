from django.urls import path

from . import api, chatwoot, core_sync, meta, views

urlpatterns = [
    path("api/v1/leads", api.create_lead, name="api_create_lead"),
    path("webhooks/core/", core_sync.core_webhook, name="core_webhook"),
    path("webhooks/chatwoot/", chatwoot.chatwoot_webhook, name="chatwoot_webhook"),
    path("webhooks/meta/", meta.meta_webhook, name="meta_webhook"),
    path("crm/", views.kanban, name="crm_kanban"),
    path("crm/leads/novo/", views.lead_new, name="crm_lead_new"),
    path("crm/leads/<uuid:lead_uuid>/", views.lead_detail, name="crm_lead"),
    path("crm/leads/<uuid:lead_uuid>/etapa/", views.lead_stage, name="crm_lead_stage"),
    path("crm/leads/<uuid:lead_uuid>/nota/", views.lead_note, name="crm_lead_note"),
]
