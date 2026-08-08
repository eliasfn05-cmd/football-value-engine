from decimal import Decimal
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from backtesting.models import PredictionOutcome
from engine.models import DailyPremiumSelection, Fixture, Prediction, Team
from engine.score_v8 import V8_MODEL_VERSION


class DashboardTests(TestCase):
    def setUp(self):
        self.home = Team.objects.create(external_id="dash-home", name="Dashboard Home")
        self.away = Team.objects.create(external_id="dash-away", name="Dashboard Away")

    def _prediction(self, *, kickoff, tier="", market="OVER_2_5", odds="1.80"):
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
            score=Decimal("92.00"),
            tier=tier,
            reasons={"v8_gates_passed": True, "data_quality_score": 85.0, "bookmaker": "Betano"},
        )

    def _select(self, prediction, *, rank=1, premium_tier="A"):
        return DailyPremiumSelection.objects.create(
            target_date=timezone.localdate(prediction.fixture.kickoff),
            prediction=prediction,
            rank=rank,
            premium_tier=premium_tier,
            premium_rank_score=Decimal("91.50"),
            model_version=V8_MODEL_VERSION,
            rationale={"test": True},
        )

    def test_health_endpoint_is_preserved(self):
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_dashboard_renders_no_bet_without_premium(self):
        response = self.client.get("/dashboard/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Premium Picks")
        self.assertContains(response, "NO BET")
        self.assertNotContains(response, "Candidatos cercanos a Premium")
        self.assertNotContains(response, "DIAGNÓSTICO")

    def test_dashboard_shows_ranked_future_premium_card(self):
        prediction = self._prediction(kickoff=timezone.now() + timedelta(hours=4))
        self._select(prediction, premium_tier="A")
        response = self.client.get("/dashboard/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Top Premium del día")
        self.assertContains(response, "PREMIUM A · #1")
        self.assertContains(response, "Dashboard Home")
        self.assertContains(response, "Dashboard Away")
        self.assertContains(response, "OVER_2_5")
        self.assertContains(response, "Copiar Picks")
        self.assertNotContains(response, "NO BET")

    def test_near_premium_is_hidden_operationally_and_visible_in_developer(self):
        prediction = self._prediction(
            kickoff=timezone.now() + timedelta(hours=3),
            tier="",
            market="BTTS",
            odds="1.70",
        )
        prediction.edge = Decimal("0.03000")
        prediction.expected_value = Decimal("0.05000")
        prediction.save(update_fields=["edge", "expected_value"])

        operational = self.client.get("/dashboard/")
        self.assertEqual(operational.status_code, 200)
        self.assertNotContains(operational, "Candidatos cercanos a Premium")

        developer = self.client.get("/developer/")
        self.assertEqual(developer.status_code, 200)
        self.assertContains(developer, "Developer Diagnostics")
        self.assertContains(developer, "Candidatos cercanos a Premium")
        self.assertContains(developer, "Edge &lt; 5%")

    def test_dashboard_metrics_use_only_operational_selections(self):
        prediction = self._prediction(kickoff=timezone.now() - timedelta(days=1))
        self._select(prediction, premium_tier="B")
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
        self.assertContains(response, "100,0%")
        self.assertContains(response, "0,80 u")
        self.assertContains(response, "WIN")
