from django.urls import path
from . import views

urlpatterns = [
    path('dashboard/', views.dashboard_view, name='dashboard'),
    path('profile/', views.profile_view, name='profile'),
    path('kanban/<path:sheet_name>/', views.kanban_view, name='kanban'),
]
