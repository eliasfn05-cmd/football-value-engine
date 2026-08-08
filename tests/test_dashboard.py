from decimal import Decimal
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from backtesting.models import PredictionOutcome
from engine.models import Fixture, Prediction, Team
from engine.score_v8 import V8_MODEL_VERSION


class DashboardTests(TestCase):
    def setUp(self):
        self.home = Team.objects.create(external_id="dash-home", name="Dashboard Home")
        self.away = Team.objects.create(external_id="dash-away", name="Dashboard Away")

    def _prediction(self, *, kickoff, tier="TIER_A", market="OVER_2_5", odds="1.80"):
        fixture = Fixture.objects.create(
            external_id=f"fixture-{Fixture.objects.count()+1}",
            competition="Dashboard League",
            kickoff=kickoff,
            home_team=self.home,
            away_team=self.away,
            status="NS",
        )
        return Prediction.objects.create(
            fixture=fixture,
            model_version=V8_MODEL_VERSION,
            market=market,
            selection="OVER" if market == "OVER_2_5" else "YES",
            probability=Decimal("0.70000"),
            fair_odds=Decimal("1.429"),
            market_odds=Decimal(odds),
            edge=Decimal("0.14444"),
            expected_value=Decimal("0.26000"),
            score=Decimal("88.00"),
            tier=tier,
            reasons={"early_season_penalty": 0.04},
        )

    def test_health_endpoint_is_preserved(self):
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_dashboard_renders_without_data(self):
        response = self.client.get("/dashboard/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dashboard Premium")
        self.assertContains(response, "No hay Picks Premium futuros almacenados todavía.")

    def test_dashboard_shows_future_premium_pick(self):
        self._prediction(kickoff=timezone.now() + timedelta(hours=4))
        response = self.client.get("/dashboard/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dashboard Home")
        self.assertContains(response, "Dashboard Away")
        self.assertContains(response, "OVER_2_5")

    def test_dashboard_uses_settled_premium_metrics(self):
        prediction = self._prediction(kickoff=timezone.now() - timedelta(days=1))
        prediction.fixture.status = "FT"
        prediction.fixture.home_goals = 2
        prediction.fixture.away_goals = 1
        prediction.fixture.save(update_fields=["status", "home_goals", "away_goals"])
        PredictionOutcome.objects.create(
            prediction=prediction,
            result=PredictionOutcome.RESULT_WIN,
            home_goals=2,
            away_goals=1,
            stake_units=Decimal("1.000"),
            profit_units=Decimal("0.8000"),
            settled_at=timezone.now(),
            settlement_reason="over_2_5",
        )

        response = self.client.get("/dashboard/")
        self.assertContains(response, "100.0%")
        self.assertContains(response, "0.80 u")
        self.assertContains(response, "WIN")
