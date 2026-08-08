import unittest

from scanner.odds import parse_quotes
from scanner.profiles import build_team_profile


def fixture(home_id, away_id, home_goals, away_goals):
    return {
        "teams": {"home": {"id": home_id}, "away": {"id": away_id}},
        "goals": {"home": home_goals, "away": away_goals},
    }


class ScannerProfileTests(unittest.TestCase):
    def test_away_profile_detects_zero_of_five_over25(self):
        team_id = 20
        history = [
            fixture(1, team_id, 1, 1),
            fixture(2, team_id, 0, 0),
            fixture(3, team_id, 2, 0),
            fixture(4, team_id, 0, 2),
            fixture(5, team_id, 1, 1),
        ]
        profile = build_team_profile(history, team_id, venue="away")
        self.assertEqual(profile.sample_size, 5)
        self.assertEqual(profile.over25_rate, 0.0)
        self.assertEqual(profile.btts_rate, 0.4)

    def test_betano_parser_returns_only_preferred_bookmaker(self):
        payload = [{
            "bookmakers": [
                {
                    "name": "OtherBook",
                    "bets": [{"name": "Both Teams Score", "values": [{"value": "Yes", "odd": "2.50"}]}],
                },
                {
                    "name": "Betano",
                    "bets": [
                        {"name": "Both Teams Score", "values": [{"value": "Yes", "odd": "1.82"}]},
                        {"name": "Goals Over/Under", "values": [{"value": "Over 2.5", "odd": "1.91"}]},
                    ],
                },
            ]
        }]
        quotes = parse_quotes(payload, "Betano")
        self.assertEqual(quotes["btts"].decimal_odds, 1.82)
        self.assertEqual(quotes["over25"].decimal_odds, 1.91)
        self.assertEqual(quotes["btts"].bookmaker, "Betano")

    def test_missing_betano_does_not_fallback_by_default(self):
        payload = [{
            "bookmakers": [{
                "name": "OtherBook",
                "bets": [{"name": "Goals Over/Under", "values": [{"value": "Over 2.5", "odd": "2.10"}]}],
            }]
        }]
        quotes = parse_quotes(payload, "Betano")
        self.assertIsNone(quotes["btts"])
        self.assertIsNone(quotes["over25"])

    def test_explicit_fallback_uses_bookmaker_with_best_target_market_coverage(self):
        payload = [{
            "bookmakers": [
                {
                    "name": "PartialBook",
                    "bets": [
                        {"name": "Goals Over/Under", "values": [{"value": "Over 2.5", "odd": "2.10"}]},
                    ],
                },
                {
                    "name": "ReferenceBook",
                    "bets": [
                        {"name": "Both Teams Score", "values": [{"value": "Yes", "odd": "1.95"}]},
                        {"name": "Goals Over/Under", "values": [{"value": "Over 2.5", "odd": "2.02"}]},
                    ],
                },
            ]
        }]
        quotes = parse_quotes(payload, "Betano", allow_fallback=True)
        self.assertEqual(quotes["btts"].decimal_odds, 1.95)
        self.assertEqual(quotes["over25"].decimal_odds, 2.02)
        self.assertEqual(quotes["btts"].bookmaker, "ReferenceBook")
        self.assertEqual(quotes["over25"].bookmaker, "ReferenceBook")


if __name__ == "__main__":
    unittest.main()
