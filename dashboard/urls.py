from django.urls import path

from .views import dashboard_home, developer_dashboard, health

urlpatterns = [
    path("health/", health, name="health"),
    path("dashboard/", dashboard_home, name="dashboard-home"),
    path("developer/", developer_dashboard, name="developer-dashboard"),
]
