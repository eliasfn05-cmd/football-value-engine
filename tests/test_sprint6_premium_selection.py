from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from engine.models import DailyPremiumSelection, Fixture, Prediction, Team
from engine.premium_selection import DailyPremiumSelector
from engine.score_v8 import V8_MODEL_VERSION


class Sprint6PremiumSelectionTests(TestCase):
    def setUp(self):
        self.home = Team.objects.create(external_id="s6-home", name="S6 Home")
        self.away = Team.objects.create(external_id="s6-away", name="S6 Away")

    def _prediction(self, index, *, market="BTTS", score="92.00", edge="0.10000", ev="0.12000", probability="0.66000", fixture=None):
        if fixture is None:
            fixture = Fixture.objects.create(
                external_id=f"s6-fixture-{index}",
                competition="Sprint 6 League",
                kickoff=timezone.now() + timedelta(hours=index + 1),
                home_team=self.home,
                away_team=self.away,
                status="NS",
            )
        return Prediction.objects.create(
            fixture=fixture,
            model_version=V8_MODEL_VERSION,
            market=market,
            selection="YES" if market == "BTTS" else "OVER",
            probability=Decimal(probability),
            fair_odds=Decimal("1.500"),
            market_odds=Decimal("2.000"),
            edge=Decimal(edge),
            expected_value=Decimal(ev),
            score=Decimal(score),
            tier="",
            reasons={
                "v8_gates_passed": True,
                "data_quality_score": 85.0,
                "bookmaker": "Betano",
            },
        )

    def test_selects_at_most_three_ranked_picks(self):
        for index in range(1, 6):
            self._prediction(index, score=str(94 - index))
        target_date = timezone.localdate()
        rows = DailyPremiumSelector().select(target_date)
        self.assertEqual(len(rows), 3)
        self.assertEqual([row.rank for row in rows], [1, 2, 3])
        self.assertEqual(DailyPremiumSelection.objects.filter(target_date=target_date).count(), 3)

    def test_never_selects_two_markets_from_same_fixture(self):
        fixture = Fixture.objects.create(
            external_id="s6-shared-fixture",
            competition="Sprint 6 League",
            kickoff=timezone.now() + timedelta(hours=3),
            home_team=self.home,
            away_team=self.away,
            status="NS",
        )
        self._prediction(1, fixture=fixture, market="BTTS", score="96.00", ev="0.15000")
        self._prediction(2, fixture=fixture, market="OVER_2_5", score="95.00", ev="0.14000", probability="0.68000")
        self._prediction(3, score="93.00")

        rows = DailyPremiumSelector().select(timezone.localdate())
        fixture_ids = [row.prediction.fixture_id for row in rows]
        self.assertEqual(len(fixture_ids), len(set(fixture_ids)))

    def test_returns_no_bet_when_candidates_miss_tier_c_floor(self):
        self._prediction(1, score="83.00", edge="0.04000", ev="0.05000", probability="0.58000")
        rows = DailyPremiumSelector().select(timezone.localdate())
        self.assertEqual(rows, [])

    def test_assigns_tier_a_b_and_c_by_threshold(self):
        a = self._prediction(1, score="93.00", edge="0.10000", ev="0.12000", probability="0.66000")
        b = self._prediction(2, score="89.00", edge="0.08000", ev="0.09000", probability="0.62000")
        c = self._prediction(3, score="85.00", edge="0.06000", ev="0.07000", probability="0.60000")
        self.assertEqual(DailyPremiumSelector._tier_for(a), "A")
        self.assertEqual(DailyPremiumSelector._tier_for(b), "B")
        self.assertEqual(DailyPremiumSelector._tier_for(c), "C")
