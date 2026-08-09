from django.urls import path

from .views import (
    dashboard_home,
    developer_dashboard,
    generate_premium_picks,
    health,
    premium_generation_status,
)

urlpatterns = [
    path("health/", health, name="health"),
    path("dashboard/", dashboard_home, name="dashboard-home"),
    path("dashboard/developer/", developer_dashboard, name="developer-dashboard"),
    path("dashboard/generate-premium/", generate_premium_picks, name="generate-premium-picks"),
    path("dashboard/generation-status/", premium_generation_status, name="premium-generation-status"),
    # Backwards-compatible alias kept temporarily.
    path("developer/", developer_dashboard, name="developer-dashboard-legacy"),
]
