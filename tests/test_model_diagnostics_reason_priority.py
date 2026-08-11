import unittest

from engine.model_diagnostics import ModelDiagnosticsService


class ModelDiagnosticsReasonPriorityTests(unittest.TestCase):
    def test_negative_value_rejection_outranks_deep_missing(self):
        reasons = ["deep_missing", "reliable_ev:-0.149", "calibrated_edge:-0.120"]
        selected = ModelDiagnosticsService._definitive_pre_deep_reason(reasons)
        self.assertEqual(selected, "calibrated_edge:-0.120")

    def test_probability_rejection_outranks_deep_missing(self):
        reasons = ["deep_missing", "raw_probability:0.510"]
        selected = ModelDiagnosticsService._definitive_pre_deep_reason(reasons)
        self.assertEqual(selected, "raw_probability:0.510")

    def test_genuine_deep_pending_remains_pending_when_no_other_blocker_exists(self):
        reasons = ["deep_missing", "score:79.0"]
        selected = ModelDiagnosticsService._definitive_pre_deep_reason(reasons)
        self.assertIsNone(selected)

    def test_structural_v8_failure_outranks_deep_missing(self):
        reasons = ["deep_missing", "v8_gates"]
        selected = ModelDiagnosticsService._definitive_pre_deep_reason(reasons)
        self.assertEqual(selected, "v8_gates")


if __name__ == "__main__":
    unittest.main()
