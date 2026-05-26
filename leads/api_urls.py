from django.urls import path
from . import views

urlpatterns = [
    path('profile/', views.profile_api, name='api_profile'),
    path('profile/test-telegram/', views.test_telegram, name='api_test_telegram'),
    path('profile/test-sheet/', views.test_sheet, name='api_test_sheet'),
    path('profile/detect-tabs/', views.detect_tabs, name='api_detect_tabs'),
    path('profile/sync-now/', views.sync_now, name='api_sync_now'),
    path('dashboard/', views.dashboard_api, name='api_dashboard'),
    path('sheets/', views.sheets_api, name='api_sheets'),
    path('kanban/<str:sheet_name>/', views.kanban_api, name='api_kanban'),
    path('lead/<int:row_index>/', views.lead_update, name='api_lead_update'),
]
