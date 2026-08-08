from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from dashboard.services import DashboardService
from engine.models import DailyPremiumSelection, Fixture, Prediction, Team
from engine.score_v8 import V8_MODEL_VERSION


class DashboardFriendlyGuardTests(TestCase):
    def test_stale_friendly_selection_is_never_returned_to_dashboard(self):
        home = Team.objects.create(external_id="guard-home", name="Montpellier")
        away = Team.objects.create(external_id="guard-away", name="Dijon")
        fixture = Fixture.objects.create(
            external_id="guard-friendly-fixture",
            competition="Friendlies Clubs",
            round="Club Friendlies",
            kickoff=timezone.now() + timedelta(hours=4),
            home_team=home,
            away_team=away,
            status="NS",
        )
        prediction = Prediction.objects.create(
            fixture=fixture,
            model_version=V8_MODEL_VERSION,
            market="OVER_2_5",
            selection="OVER",
            probability=Decimal("0.78600"),
            fair_odds=Decimal("1.272"),
            market_odds=Decimal("2.200"),
            edge=Decimal("0.33100"),
            expected_value=Decimal("0.72900"),
            score=Decimal("100.00"),
            tier="TIER_A",
            reasons={"v8_gates_passed": True, "data_quality_score": 95.0, "bookmaker": "Betano"},
        )
        DailyPremiumSelection.objects.create(
            target_date=timezone.localdate(fixture.kickoff),
            prediction=prediction,
            rank=1,
            premium_tier="A",
            premium_rank_score=Decimal("97.80"),
            model_version=V8_MODEL_VERSION,
        )

        picks = DashboardService().premium_picks()

        self.assertEqual(picks, [])
        response = self.client.get("/dashboard/")
        self.assertContains(response, "NO BET")
        self.assertNotContains(response, "Montpellier")
        self.assertNotContains(response, "Dijon")
