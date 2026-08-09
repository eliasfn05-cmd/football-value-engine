from django.test import SimpleTestCase

from engine.features import FeatureVector, VenueProfile
from engine.market_intelligence import MarketIntelligenceService


class MarketIntelligenceTests(SimpleTestCase):
    def _features(self, home: VenueProfile, away: VenueProfile, *, btts_odds=1.85, over_odds=1.95):
        return FeatureVector(
            fixture_id="mi-test",
            home_team="Home",
            away_team="Away",
            home_profile=home,
            away_profile=away,
            home_over25_last5_home=home.over25_rate,
            away_over25_last5_away=away.over25_rate,
            home_btts_last5_home=home.btts_rate,
            away_btts_last5_away=away.btts_rate,
            home_clean_sheet_rate=home.clean_sheet_rate,
            away_clean_sheet_rate=away.clean_sheet_rate,
            home_failed_to_score_rate=home.failed_to_score_rate,
            away_failed_to_score_rate=away.failed_to_score_rate,
            home_table_position=4,
            away_table_position=6,
            home_points_per_game=1.7,
            away_points_per_game=1.5,
            home_lineup_continuity=None,
            away_lineup_continuity=None,
            btts_market_odds=btts_odds,
            over25_market_odds=over_odds,
            data_quality_score=80.0,
        )

    def test_prefers_btts_when_btts_is_high_but_escalation_is_low(self):
        home = VenueProfile(5, 1.4, 1.2, 0.40, 0.60, 0.20, 0.20, 0.33, 0.60, 0.40)
        away = VenueProfile(5, 1.2, 1.3, 0.40, 0.60, 0.20, 0.20, 0.33, 0.60, 0.40)
        features = self._features(home, away)
        service = MarketIntelligenceService()

        over = service.evaluate(features, "OVER_2_5")
        btts = service.evaluate(features, "BTTS")

        self.assertFalse(over.passed)
        self.assertIn("prefer_btts_over_over25", over.failures)
        self.assertTrue(btts.passed)
        self.assertGreater(btts.score, over.score)

    def test_rejects_over_when_low_score_script_is_strong(self):
        home = VenueProfile(5, 1.0, 0.9, 0.20, 0.40, 0.40, 0.20, 0.50, 0.80, 0.20)
        away = VenueProfile(5, 0.8, 1.0, 0.20, 0.40, 0.30, 0.30, 0.50, 0.80, 0.20)
        features = self._features(home, away)

        result = MarketIntelligenceService().evaluate(features, "OVER_2_5")

        self.assertFalse(result.passed)
        self.assertIn("strong_low_score_script", result.failures)
        self.assertGreaterEqual(result.evidence["combined_low_score_rate"], 0.60)
        self.assertLess(result.evidence["combined_avg_total_goals"], 2.35)

    def test_open_high_escalation_profile_keeps_over_eligible(self):
        home = VenueProfile(5, 2.0, 1.4, 0.80, 0.70, 0.10, 0.10, 0.85, 0.20, 0.00)
        away = VenueProfile(5, 1.8, 1.5, 0.80, 0.70, 0.10, 0.10, 0.85, 0.20, 0.00)
        features = self._features(home, away)

        result = MarketIntelligenceService().evaluate(features, "OVER_2_5")

        self.assertTrue(result.passed)
        self.assertGreaterEqual(result.score, 65.0)
        self.assertNotIn("prefer_btts_over_over25", result.failures)

    def test_missing_samples_stay_neutral_instead_of_hard_rejecting(self):
        home = VenueProfile(1, 1.2, 1.2, 0.50, 0.50, 0.20, 0.20)
        away = VenueProfile(1, 1.2, 1.2, 0.50, 0.50, 0.20, 0.20)
        features = self._features(home, away)

        result = MarketIntelligenceService().evaluate(features, "OVER_2_5")

        self.assertNotIn("strong_low_score_script", result.failures)
        self.assertNotIn("prefer_btts_over_over25", result.failures)
