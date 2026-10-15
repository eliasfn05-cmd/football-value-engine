import unittest
from over15_risk_gate import TeamEvidence, assess_over15, poisson_over15


def evidence(score=(2, 1)):
    return TeamEvidence([score] * 10, [score] * 10)


class RiskGateTests(unittest.TestCase):
    def test_moss_like_high_average_but_insufficient_lambda(self):
        d = assess_over15(evidence(), evidence(), total_lambda=2.60, calibrated=True, h2h_recent=[(2, 1)])
        self.assertFalse(d.approved)
        self.assertTrue(any("lambda" in r for r in d.reasons))

    def test_recent_zero_zero_is_hard_veto(self):
        bad = TeamEvidence([(0, 0)] + [(3, 1)] * 9, [(0, 0)] + [(3, 1)] * 9)
        d = assess_over15(bad, evidence(), total_lambda=3.8, calibrated=True, h2h_recent=[(2, 2)])
        self.assertFalse(d.approved)

    def test_missing_data_fail_closed(self):
        d = assess_over15(TeamEvidence([], []), evidence(), total_lambda=None, calibrated=False)
        self.assertFalse(d.approved)

    def test_strong_case_can_pass_but_not_ev_certified(self):
        d = assess_over15(evidence(), evidence(), total_lambda=3.5, calibrated=True, h2h_recent=[(2, 1)])
        self.assertTrue(d.approved)
        self.assertEqual(d.tier, "CANDIDATO_APTO_NO_EV")

    def test_poisson(self):
        self.assertAlmostEqual(poisson_over15(2.6), 0.732615, places=5)


if __name__ == "__main__":
    unittest.main()
