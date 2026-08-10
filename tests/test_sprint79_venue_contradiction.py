from types import SimpleNamespace

from django.test import SimpleTestCase

from engine.premium_selection import DailyPremiumSelector


class Sprint79VenueContradictionTests(SimpleTestCase):
    def _prediction(self, market, evidence):
        return SimpleNamespace(
            market=market,
            reasons={"deep_analysis_evidence": evidence},
        )

    def test_over25_blocks_mura_style_home_venue_contradiction(self):
        prediction = self._prediction(
            "OVER_2_5",
            {
                "home_sample": 10,
                "away_sample": 10,
                "home_recent_n": 5,
                "away_recent_n": 5,
                "home_over25_rate": 0.40,
                "away_over25_rate": 0.80,
                "home_recent_over25_rate": 0.40,
                "away_recent_over25_rate": 0.80,
            },
        )
        metrics = DailyPremiumSelector._venue_contradiction_metrics(prediction)
        self.assertTrue(metrics["blocked"])
        self.assertEqual(metrics["weak_side"], "home")
        self.assertGreater(metrics["rank_penalty"], 0.0)

    def test_over25_does_not_block_two_balanced_strong_venue_profiles(self):
        prediction = self._prediction(
            "OVER_2_5",
            {
                "home_sample": 10,
                "away_sample": 10,
                "home_recent_n": 5,
                "away_recent_n": 5,
                "home_over25_rate": 0.70,
                "away_over25_rate": 0.70,
                "home_recent_over25_rate": 0.60,
                "away_recent_over25_rate": 0.80,
            },
        )
        metrics = DailyPremiumSelector._venue_contradiction_metrics(prediction)
        self.assertFalse(metrics["blocked"])
        self.assertLess(metrics["rank_penalty"], 2.0)

    def test_btts_blocks_one_sided_home_away_contradiction(self):
        prediction = self._prediction(
            "BTTS",
            {
                "home_sample": 10,
                "away_sample": 10,
                "home_recent_n": 5,
                "away_recent_n": 5,
                "home_btts_rate": 0.40,
                "away_btts_rate": 0.80,
                "home_recent_btts_rate": 0.40,
                "away_recent_btts_rate": 0.80,
                "home_recent_failed_to_score_rate": 0.40,
                "away_recent_failed_to_score_rate": 0.10,
            },
        )
        metrics = DailyPremiumSelector._venue_contradiction_metrics(prediction)
        self.assertTrue(metrics["blocked"])
        self.assertEqual(metrics["weak_side"], "home")
