from types import SimpleNamespace

from django.test import SimpleTestCase

from engine.probability_calibration import ProbabilityEVCalibrationService


class ProbabilityCalibrationTests(SimpleTestCase):
    def setUp(self):
        self.service = ProbabilityEVCalibrationService()

    @staticmethod
    def _prediction(*, probability, odds, support, coverage=1.0, penalty=0.0, quality=90.0):
        return SimpleNamespace(
            probability=probability,
            market_odds=odds,
            reasons={
                "data_quality_score": quality,
                "venue_sample_confidence": coverage,
                "deep_analysis_evidence": {
                    "market_support_index": support,
                    "sample_coverage": coverage,
                    "total_deep_penalty": penalty,
                },
            },
        )

    def test_weak_evidence_shrinks_inflated_probability_and_ev(self):
        prediction = self._prediction(
            probability=0.82,
            odds=1.90,
            support=0.52,
            coverage=0.60,
            penalty=12.0,
            quality=60.0,
        )
        result = self.service.calibrate(prediction)

        self.assertLess(result.calibrated_probability, result.raw_probability)
        self.assertLess(result.calibrated_ev, result.raw_ev)
        self.assertLess(result.reliable_ev, result.calibrated_ev)
        self.assertGreater(result.probability_shrinkage, 0.0)
        self.assertFalse(result.premium_reliable)

    def test_strong_evidence_preserves_more_of_model_advantage(self):
        weak = self.service.calibrate(
            self._prediction(
                probability=0.76,
                odds=1.90,
                support=0.52,
                coverage=0.60,
                penalty=10.0,
                quality=60.0,
            )
        )
        strong = self.service.calibrate(
            self._prediction(
                probability=0.76,
                odds=1.90,
                support=0.82,
                coverage=1.0,
                penalty=0.0,
                quality=95.0,
            )
        )

        self.assertGreater(strong.reliability, weak.reliability)
        self.assertGreater(strong.calibrated_probability, weak.calibrated_probability)
        self.assertGreater(strong.reliable_ev, weak.reliable_ev)
        self.assertTrue(strong.premium_reliable)
        self.assertTrue(strong.tier_a_reliable)

    def test_calibration_never_creates_value_when_model_is_below_market(self):
        prediction = self._prediction(
            probability=0.50,
            odds=1.80,
            support=0.90,
            coverage=1.0,
            penalty=0.0,
            quality=95.0,
        )
        result = self.service.calibrate(prediction)

        self.assertEqual(result.calibrated_probability, result.raw_probability)
        self.assertLess(result.calibrated_edge, 0.0)
        self.assertLess(result.calibrated_ev, 0.0)
        self.assertEqual(result.reliable_ev, 0.0)

    def test_probability_advantage_is_capped_even_with_extreme_raw_probability(self):
        prediction = self._prediction(
            probability=0.95,
            odds=2.00,
            support=0.90,
            coverage=1.0,
            penalty=0.0,
            quality=100.0,
        )
        result = self.service.calibrate(prediction)

        self.assertLess(result.calibrated_probability, 0.95)
        self.assertLessEqual(result.capped_probability_advantage, 0.1535)
        self.assertLess(result.calibrated_ev, result.raw_ev)
