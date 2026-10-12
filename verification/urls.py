from django.urls import path

from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('cases/new/', views.case_create, name='case_create'),
    path('cases/<int:pk>/', views.case_detail, name='case_detail'),
    path('cases/<int:pk>/evidence/add/', views.case_add_evidence, name='case_add_evidence'),
    path('cases/<int:pk>/search/add/', views.case_add_search, name='case_add_search'),
    path('cases/<int:pk>/status/', views.case_update_status, name='case_update_status'),
    path('cases/<int:pk>/analyze/', views.case_analyze, name='case_analyze'),
    path('cases/<int:pk>/delete/', views.case_delete, name='case_delete'),
    path('cases/<int:pk>/report/', views.case_report, name='case_report'),
    path('guide/', views.guide, name='guide'),
]