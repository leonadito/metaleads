from django.urls import path

from . import api, chatwoot, core_sync, google_ads, messages_views, meta, tracking, views

urlpatterns = [
    path("api/v1/leads", api.create_lead, name="api_create_lead"),
    path("webhooks/core/", core_sync.core_webhook, name="core_webhook"),
    path("webhooks/chatwoot/", chatwoot.chatwoot_webhook, name="chatwoot_webhook"),
    path("webhooks/meta/", meta.meta_webhook, name="meta_webhook"),
    path("webhooks/google-ads/", google_ads.google_ads_webhook, name="google_ads_webhook"),
    path("t/<str:token>/", tracking.click, name="tracked_click"),
    path("o/<uuid:message_uuid>.gif", tracking.open_pixel, name="tracked_open"),
    path("descadastro/<uuid:message_uuid>/", tracking.unsubscribe, name="unsubscribe"),
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
    path("crm/funil/prompt-mestre/", views.stage_prompt_save, name="crm_stage_prompt_save"),
    path("crm/funil/ia/", views.ai_switch, name="crm_ai_switch"),
    path("crm/mensagens/", messages_views.messages_page, name="crm_messages"),
    path("crm/mensagens/limites/", messages_views.limits_save, name="crm_messages_limits"),
    path("crm/mensagens/inicial/", messages_views.initial_save, name="crm_initial_save"),
    path("crm/mensagens/bloqueio/", messages_views.suppression_add, name="crm_suppression_add"),
    path("crm/mensagens/retomar-email/", messages_views.email_resume, name="crm_email_resume"),
    path("crm/mensagens/bloqueio/<int:pk>/remover/", messages_views.suppression_remove, name="crm_suppression_remove"),
    path("crm/mensagens/<uuid:message_uuid>/reenviar/", messages_views.message_resend, name="crm_message_resend"),
]
