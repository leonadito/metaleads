from django.urls import path

from . import api, chatwoot, core_sync, google_ads, meta, views

urlpatterns = [
    path("api/v1/leads", api.create_lead, name="api_create_lead"),
    path("webhooks/core/", core_sync.core_webhook, name="core_webhook"),
    path("webhooks/chatwoot/", chatwoot.chatwoot_webhook, name="chatwoot_webhook"),
    path("webhooks/meta/", meta.meta_webhook, name="meta_webhook"),
    path("webhooks/google-ads/", google_ads.google_ads_webhook, name="google_ads_webhook"),
    path("crm/", views.kanban, name="crm_kanban"),
    path("crm/leads/novo/", views.lead_new, name="crm_lead_new"),
    path("crm/leads/<uuid:lead_uuid>/", views.lead_detail, name="crm_lead"),
    path("crm/leads/<uuid:lead_uuid>/etapa/", views.lead_stage, name="crm_lead_stage"),
    path("crm/leads/<uuid:lead_uuid>/nota/", views.lead_note, name="crm_lead_note"),
    path("crm/leads/<uuid:lead_uuid>/liberar/", views.lead_release, name="crm_lead_release"),
    path("crm/funil/", views.funnel_settings, name="crm_funnel"),
    path("crm/funil/subetapa/", views.substage_save, name="crm_substage_save"),
    path("crm/funil/subetapa/<int:pk>/ativa/", views.substage_toggle, name="crm_substage_toggle"),
    path("crm/funil/subetapa/<int:pk>/ordem/", views.substage_reorder, name="crm_substage_reorder"),
    path("crm/funil/aviso/", views.waiting_settings, name="crm_waiting_settings"),
]
