import unittest
from dataclasses import replace
from datetime import date
from premium_over15_gate import MatchEvidence, TeamEvidence, evaluate_over15_premium


class KnockoutContextVetoTests(unittest.TestCase):
    def test_columbus_st_louis_cup_final_cannot_be_premium(self):
        result = evaluate_over15_premium(MatchEvidence(
            home=TeamEvidence(), away=TeamEvidence(), is_cup_final=True), date(2026, 10, 21))
        self.assertFalse(result.approved)
        self.assertEqual(result.tier, "NO_BET")
        self.assertTrue(any("Final de copa" in r for r in result.reasons))

    def test_firpo_saprissa_first_leg_cannot_be_premium(self):
        result = evaluate_over15_premium(MatchEvidence(
            home=TeamEvidence(), away=TeamEvidence(), is_two_leg_first_match=True), date(2026, 10, 21))
        self.assertFalse(result.approved)
        self.assertEqual(result.tier, "NO_BET")
        self.assertTrue(any("Ida de eliminatoria" in r for r in result.reasons))


if __name__ == "__main__":
    unittest.main()
