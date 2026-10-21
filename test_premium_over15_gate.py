import unittest
from datetime import date
from premium_over15_gate import TeamEvidence, MatchEvidence, evaluate_over15_premium

D = date(2026, 10, 21)
GOOD = TeamEvidence(
    overall_over15_last5=5, venue_over15_last5=5, venue_over25_last5=4,
    scored_last5=5, scored_last10=9, overall_goals_for_avg=1.7,
    venue_goals_for_avg=1.8, overall_total_goals_avg=3.3,
    venue_total_goals_avg=3.4, failed_to_score_last10=1)
def fixture(**overrides):
    values = dict(home=GOOD, away=GOOD, lambda_home=1.7, lambda_away=1.7,
                  p_over15=.88, p_btts=.68, latest_h2h_total_goals=3,
                  h2h_under15_last5=0, consecutive_h2h_one_team_fts=False,
                  data_as_of=D)
    values.update(overrides)
    return MatchEvidence(**values)

class PremiumGateTests(unittest.TestCase):
    def test_full_evidence_passes(self):
        self.assertEqual(evaluate_over15_premium(fixture(), D).tier, "A")
    def test_fc_tokyo_kashiwa_external_probability_alone_is_not_enough(self):
        e = MatchEvidence(TeamEvidence(), TeamEvidence(), p_over15=.915)
        self.assertEqual(evaluate_over15_premium(e, D).tier, "NO_BET")
    def test_latest_h2h_0_0_is_veto(self):
        self.assertFalse(evaluate_over15_premium(fixture(latest_h2h_total_goals=0), D).approved)
    def test_weaker_team_lambda_is_veto(self):
        self.assertFalse(evaluate_over15_premium(fixture(lambda_away=1.1), D).approved)
    def test_stale_data_is_veto(self):
        self.assertFalse(evaluate_over15_premium(fixture(data_as_of=date(2026,10,16)), D).approved)
    def test_not_5_of_5_venue_is_veto(self):
        bad = TeamEvidence(**{**GOOD.__dict__, "venue_over15_last5":4})
        self.assertFalse(evaluate_over15_premium(fixture(away=bad), D).approved)

if __name__ == "__main__":
    unittest.main()
