from django.urls import path

from .views import dashboard_home, health

urlpatterns = [
    path("health/", health, name="health"),
    path("dashboard/", dashboard_home, name="dashboard-home"),
]
