from django.urls import path

from .backtest_views import btts_v29_vs_v291_backtest
from .cancel_generation import cancel_premium_generation
from .views import (
    dashboard_home,
    developer_dashboard,
    health,
    premium_generation_status,
)
from .window_generation import generate_premium_picks_windowed

urlpatterns = [
    path("health/", health, name="health"),
    path("dashboard/", dashboard_home, name="dashboard-home"),
    path("dashboard/developer/", developer_dashboard, name="developer-dashboard"),
    path("dashboard/generate-premium/", generate_premium_picks_windowed, name="generate-premium-picks"),
    path("dashboard/cancel-premium/", cancel_premium_generation, name="cancel-premium-generation"),
    path("dashboard/generation-status/", premium_generation_status, name="premium-generation-status"),
    path("dashboard/backtest/btts-v29-v291/", btts_v29_vs_v291_backtest, name="btts-v29-v291-backtest"),
    # Backwards-compatible alias kept temporarily.
    path("developer/", developer_dashboard, name="developer-dashboard-legacy"),
]
