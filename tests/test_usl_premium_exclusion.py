from datetime import datetime, time, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from engine.competition_quality import classify_competition
from engine.deep_analysis import DEEP_ANALYSIS_VERSION
from engine.models import Competition, DailyPremiumSelection, Fixture, Prediction, Team
from engine.premium_selection import DailyPremiumSelector
from engine.score_v8 import V8_MODEL_VERSION


class USLCupPremiumExclusionTests(TestCase):
    def setUp(self):
        self.home = Team.objects.create(external_id="greenville-test", name="Greenville Triumph", country="USA")
        self.away = Team.objects.create(external_id="knoxville-test", name="One Knoxville", country="USA")
        self.target_date = timezone.localdate() + timedelta(days=1)
        self.kickoff = timezone.make_aware(datetime.combine(self.target_date, time(18, 0)))

    def _fixture(self):
        competition = Competition.objects.create(
            external_id="usl-cup-test",
            name="USL Cup - Playoffs",
            country="USA",
            season=2026,
            competition_type="Cup",
        )
        return Fixture.objects.create(
            external_id="greenville-knoxville-test",
            competition="USL Cup - Playoffs",
            competition_ref=competition,
            season=2026,
            round="Playoffs",
            kickoff=self.kickoff,
            home_team=self.home,
            away_team=self.away,
            status="NS",
        )

    def _prediction(self, fixture):
        return Prediction.objects.create(
            fixture=fixture,
            model_version=V8_MODEL_VERSION,
            market="OVER_2_5",
            selection="OVER",
            probability=Decimal("0.69100"),
            fair_odds=Decimal("1.447"),
            market_odds=Decimal("1.620"),
            edge=Decimal("0.07400"),
            expected_value=Decimal("0.11900"),
            score=Decimal("79.88"),
            tier="TIER_B",
            reasons={
                "v8_gates_passed": True,
                "deep_analysis_version": DEEP_ANALYSIS_VERSION,
                "deep_analysis_passed": True,
                "deep_preferred_market": True,
                "deep_score": 79.88,
            },
        )

    def test_usl_cup_is_hard_excluded(self):
        fixture = self._fixture()
        quality = classify_competition(fixture)
        self.assertTrue(quality.excluded)
        self.assertEqual(quality.reason, "lower_league_liquidity_filter")

    def test_selector_cannot_publish_usl_cup_even_with_value_numbers(self):
        fixture = self._fixture()
        self._prediction(fixture)
        selections = DailyPremiumSelector().select(self.target_date)
        self.assertEqual(selections, [])
        self.assertFalse(DailyPremiumSelection.objects.filter(target_date=self.target_date).exists())
