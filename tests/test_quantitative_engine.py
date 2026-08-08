import math
import unittest

from engine.model import FootballValueEngine
from engine.quantitative import (
    MarketQuote,
    MatchContext,
    TeamProfile,
    expected_value,
    implied_probability,
    probability_btts,
    probability_over_25,
)


class QuantitativePrimitivesTests(unittest.TestCase):
    def test_poisson_over_25_known_value(self):
        self.assertAlmostEqual(probability_over_25(3.0), 0.5768099, places=5)

    def test_btts_probability(self):
        expected = (1 - math.exp(-1.5)) * (1 - math.exp(-1.2))
        self.assertAlmostEqual(probability_btts(1.5, 1.2), expected, places=7)

    def test_market_math(self):
        self.assertAlmostEqual(implied_probability(2.0), 0.5)
        self.assertAlmostEqual(expected_value(0.60, 1.90), 0.14)


class FootballValueEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = FootballValueEngine()
        self.home = TeamProfile(
            goals_for=2.10,
            goals_against=1.60,
            xg_for=2.00,
            xg_against=1.50,
            over25_rate=0.70,
            btts_rate=0.70,
            clean_sheet_rate=0.20,
            failed_to_score_rate=0.10,
        )
        self.away = TeamProfile(
            goals_for=1.80,
            goals_against=1.70,
            xg_for=1.70,
            xg_against=1.60,
            over25_rate=0.70,
            btts_rate=0.70,
            clean_sheet_rate=0.15,
            failed_to_score_rate=0.15,
        )

    def test_high_value_match_can_reach_tier_a(self):
        context = MatchContext(
            home=self.home,
            away=self.away,
            round_number=8,
            home_over25_last5_home=0.80,
            away_over25_last5_away=0.80,
            home_btts_last5_home=0.80,
            away_btts_last5_away=0.80,
            tactical_pace_score=0.75,
        )
        result = self.engine.evaluate(
            context,
            btts_quote=MarketQuote(1.85, "Betano"),
            over25_quote=MarketQuote(1.85, "Betano"),
        )
        self.assertEqual(result["over25"].tier, "TIER_A")
        self.assertGreaterEqual(result["over25"].edge, 0.06)
        self.assertGreaterEqual(result["over25"].expected_value, 0.08)

    def test_ahpc_away_under_pattern_reduces_over_probability(self):
        normal = MatchContext(
            home=self.home,
            away=self.away,
            round_number=8,
            home_over25_last5_home=0.80,
            away_over25_last5_away=0.80,
        )
        suppressed = MatchContext(
            home=self.home,
            away=self.away,
            round_number=8,
            home_over25_last5_home=0.80,
            away_over25_last5_away=0.00,
        )
        normal_result = self.engine.evaluate(normal)["over25"]
        suppressed_result = self.engine.evaluate(suppressed)["over25"]
        self.assertLess(suppressed_result.probability, normal_result.probability)
        self.assertGreaterEqual(normal_result.probability - suppressed_result.probability, 0.06)

    def test_low_price_can_remove_tier_a(self):
        context = MatchContext(
            home=self.home,
            away=self.away,
            round_number=8,
            home_over25_last5_home=0.80,
            away_over25_last5_away=0.80,
            tactical_pace_score=0.75,
        )
        result = self.engine.evaluate(context, over25_quote=MarketQuote(1.45, "Betano"))["over25"]
        self.assertNotEqual(result.tier, "TIER_A")
        self.assertLess(result.expected_value, 0.08)

    def test_first_round_is_penalized(self):
        round_one = MatchContext(home=self.home, away=self.away, round_number=1)
        mature = MatchContext(home=self.home, away=self.away, round_number=8)
        first = self.engine.evaluate(round_one)["over25"]
        later = self.engine.evaluate(mature)["over25"]
        self.assertLess(first.probability, later.probability)
        self.assertEqual(first.reasons["round_number"], 1)


if __name__ == "__main__":
    unittest.main()
