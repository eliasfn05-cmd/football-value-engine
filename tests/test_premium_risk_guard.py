from types import SimpleNamespace

from django.test import SimpleTestCase

from engine.premium_risk_guard import PremiumRiskGuard


def prediction(market, evidence):
    return SimpleNamespace(
        market=market,
        reasons={"deep_analysis_evidence": evidence},
    )


class PremiumRiskGuardTests(SimpleTestCase):
    def test_over25_blocks_two_of_five_away_profile(self):
        pick = prediction("OVER_2_5", {
            "home_recent_n": 5,
            "away_recent_n": 5,
            "home_recent_over25_rate": 0.80,
            "away_recent_over25_rate": 0.40,
        })
        decision = PremiumRiskGuard.evaluate(pick)
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.code, "venue_recent_over25_hard_floor")
        self.assertIn("away", decision.detail)

    def test_over25_allows_both_sides_at_half_or_better(self):
        pick = prediction("OVER_2_5", {
            "home_recent_n": 5,
            "away_recent_n": 5,
            "home_recent_over25_rate": 0.60,
            "away_recent_over25_rate": 0.60,
        })
        self.assertFalse(PremiumRiskGuard.evaluate(pick).blocked)

    def test_btts_blocks_two_of_five_venue_profile(self):
        pick = prediction("BTTS", {
            "home_recent_n": 5,
            "away_recent_n": 5,
            "home_recent_btts_rate": 0.60,
            "away_recent_btts_rate": 0.40,
            "home_recent_failed_to_score_rate": 0.20,
            "away_recent_failed_to_score_rate": 0.20,
            "home_clean_sheet_rate": 0.20,
            "away_clean_sheet_rate": 0.20,
        })
        decision = PremiumRiskGuard.evaluate(pick)
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.code, "venue_recent_btts_hard_floor")

    def test_btts_blocks_repeated_failure_to_score(self):
        pick = prediction("BTTS", {
            "home_recent_n": 5,
            "away_recent_n": 5,
            "home_recent_btts_rate": 0.60,
            "away_recent_btts_rate": 0.60,
            "home_recent_failed_to_score_rate": 0.40,
            "away_recent_failed_to_score_rate": 0.00,
            "home_clean_sheet_rate": 0.20,
            "away_clean_sheet_rate": 0.20,
        })
        decision = PremiumRiskGuard.evaluate(pick)
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.code, "home_recent_scoring_fragility")

    def test_btts_compound_nil_risk_blocks_one_of_five_fts_against_strong_defence(self):
        pick = prediction("BTTS", {
            "home_recent_n": 5,
            "away_recent_n": 5,
            "home_recent_btts_rate": 0.60,
            "away_recent_btts_rate": 0.60,
            "home_recent_failed_to_score_rate": 0.20,
            "away_recent_failed_to_score_rate": 0.00,
            "home_clean_sheet_rate": 0.20,
            "away_clean_sheet_rate": 0.50,
        })
        decision = PremiumRiskGuard.evaluate(pick)
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.code, "btts_nil_risk_home")
