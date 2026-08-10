from types import SimpleNamespace

from django.test import SimpleTestCase

from engine.premium_selection import DailyPremiumSelector


class Sprint79PremiumReasonTests(SimpleTestCase):
    def test_venue_contradiction_reason_contains_weak_side(self):
        prediction = SimpleNamespace(
            market="OVER_2_5",
            reasons={
                "deep_analysis_evidence": {
                    "home_recent_n": 5,
                    "away_recent_n": 5,
                    "home_over25_rate": 0.40,
                    "away_over25_rate": 0.80,
                    "home_recent_over25_rate": 0.40,
                    "away_recent_over25_rate": 0.80,
                }
            },
        )
        metrics = DailyPremiumSelector._venue_contradiction_metrics(prediction)
        self.assertTrue(metrics["blocked"])
        self.assertEqual(metrics["weak_side"], "home")
        self.assertEqual(metrics["weak_recent_rate"], 0.4)
        self.assertEqual(metrics["strong_recent_rate"], 0.8)
