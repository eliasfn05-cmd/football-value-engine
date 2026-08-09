from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from engine.deep_analysis import DEEP_ANALYSIS_VERSION
from engine.models import Fixture, Prediction, Team
from engine.premium_selection import DailyPremiumSelector
from engine.score_v8 import V8_MODEL_VERSION


class Sprint6PremiumSelectionTests(TestCase):
    def setUp(self):
        self.home = Team.objects.create(external_id="s6-home", name="S6 Home")
        self.away = Team.objects.create(external_id="s6-away", name="S6 Away")

    def _prediction(
        self,
        index,
        *,
        market="BTTS",
        score="92.00",
        edge="0.10000",
        ev="0.12000",
        probability="0.66000",
        odds="2.000",
        fixture=None,
        preferred=True,
    ):
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
            market_odds=Decimal(odds),
            edge=Decimal(edge),
            expected_value=Decimal(ev),
            score=Decimal(score),
            tier="",
            reasons={
                "v8_gates_passed": True,
                "data_quality_score": 85.0,
                "bookmaker": "Betano",
                "deep_analysis_version": DEEP_ANALYSIS_VERSION,
                "deep_analysis_passed": True,
                "deep_preferred_market": preferred,
                "deep_score": float(score),
            },
        )

    def test_selects_at_most_three_ranked_picks(self):
        for index in range(1, 6):
            self._prediction(index, score=str(94 - index))
        rows = DailyPremiumSelector().select(timezone.localdate())
        self.assertEqual(len(rows), 3)
        self.assertEqual([row.rank for row in rows], [1, 2, 3])

    def test_only_deep_preferred_market_can_be_selected(self):
        fixture = Fixture.objects.create(
            external_id="s7-shared-fixture",
            competition="Sprint 7 League",
            kickoff=timezone.now() + timedelta(hours=3),
            home_team=self.home,
            away_team=self.away,
            status="NS",
        )
        btts = self._prediction(1, fixture=fixture, market="BTTS", score="94.00", preferred=False)
        over = self._prediction(2, fixture=fixture, market="OVER_2_5", score="91.00", probability="0.68000", preferred=True)
        self.assertIsNone(DailyPremiumSelector._tier_for(btts))
        self.assertIsNotNone(DailyPremiumSelector._tier_for(over))

    def test_returns_no_bet_when_candidate_misses_hard_floors(self):
        self._prediction(1, score="79.00", edge="0.04000", ev="0.02000", probability="0.58000")
        rows = DailyPremiumSelector().select(timezone.localdate())
        self.assertEqual(rows, [])

    def test_assigns_only_tier_a_or_b(self):
        a = self._prediction(1, score="93.00", edge="0.10000", ev="0.12000", probability="0.66000")
        b = self._prediction(2, score="89.00", edge="0.06000", ev="0.05000", probability="0.62000")
        self.assertEqual(DailyPremiumSelector._tier_for(a), "A")
        self.assertEqual(DailyPremiumSelector._tier_for(b), "B")

    def test_low_odds_140_never_reaches_premium_even_with_high_probability(self):
        low_odds = self._prediction(
            1,
            score="96.00",
            edge="0.15000",
            ev="0.20000",
            probability="0.87000",
            odds="1.400",
        )
        self.assertIsNone(DailyPremiumSelector._tier_for(low_odds))
        self.assertEqual(DailyPremiumSelector().select(timezone.localdate()), [])

    def test_premium_odds_bounds_are_inclusive(self):
        lower = self._prediction(1, odds="1.600", score="91.00", edge="0.08000", ev="0.09000")
        upper = self._prediction(2, odds="2.400", score="91.00", edge="0.08000", ev="0.09000")
        self.assertIsNotNone(DailyPremiumSelector._tier_for(lower))
        self.assertIsNotNone(DailyPremiumSelector._tier_for(upper))
