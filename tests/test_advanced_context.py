from __future__ import annotations

import unittest
from datetime import date

from engine.quantitative import MatchContext, TeamProfile
from scanner.context import enrich_match_context, parse_round_number
from scanner.providers.base import SportsDataProvider


class FakeProvider(SportsDataProvider):
    def fixtures_by_date(self, target_date: date):
        return []

    def team_recent_fixtures(self, team_id, *, last=10):
        return []

    def head_to_head(self, home_team_id, away_team_id, *, last=5):
        return []

    def fixture_odds(self, fixture_id):
        return []

    def team_fixtures_between(self, team_id, start, end):
        if str(team_id) == "2":
            return [
                {
                    "fixture": {"id": 90, "date": "2026-08-05T18:00:00+00:00"},
                    "league": {"name": "UEFA Europa League"},
                },
                {
                    "fixture": {"id": 100, "date": "2026-08-08T18:00:00+00:00"},
                    "league": {"name": "Ekstraklasa"},
                },
                {
                    "fixture": {"id": 110, "date": "2026-08-11T18:00:00+00:00"},
                    "league": {"name": "UEFA Europa League"},
                },
            ]
        return []

    def fixture_lineups(self, fixture_id):
        if int(fixture_id) == 100:
            return [
                {
                    "team": {"id": 1},
                    "startXI": [{"player": {"id": n, "pos": "F" if n in {9, 10} else "M"}} for n in range(1, 12)],
                },
                {
                    "team": {"id": 2},
                    "startXI": [{"player": {"id": n + 20, "pos": "F" if n in {9, 10} else "M"}} for n in range(1, 12)],
                },
            ]
        if int(fixture_id) == 80:
            return [
                {
                    "team": {"id": 1},
                    "startXI": [{"player": {"id": n, "pos": "F" if n in {9, 10} else "M"}} for n in range(1, 12)],
                },
                {
                    "team": {"id": 2},
                    "startXI": [{"player": {"id": n, "pos": "F" if n in {9, 10, 11} else "M"}} for n in range(21, 32)],
                },
            ]
        return []


def profile():
    return TeamProfile(
        goals_for=1.5,
        goals_against=1.2,
        xg_for=1.5,
        xg_against=1.2,
        over25_rate=0.6,
        btts_rate=0.6,
        clean_sheet_rate=0.2,
        failed_to_score_rate=0.2,
        sample_size=5,
    )


class AdvancedContextTests(unittest.TestCase):
    def setUp(self):
        self.raw = {
            "fixture": {
                "id": 100,
                "date": "2026-08-08T18:00:00+00:00",
                "venue": {"name": "Example Stadium", "city": "Radom"},
            },
            "league": {"name": "Ekstraklasa", "round": "Regular Season - 3"},
            "teams": {"home": {"id": 1, "name": "Home"}, "away": {"id": 2, "name": "Away"}},
        }
        self.context = MatchContext(home=profile(), away=profile())

    def test_round_number_is_detected(self):
        self.assertEqual(parse_round_number(self.raw), 3)

    def test_europe_sandwich_is_detected_for_away_team(self):
        enriched, metadata = enrich_match_context(
            FakeProvider(),
            self.raw,
            self.context,
            home_history=[{"fixture": {"id": 80}}],
            away_history=[{"fixture": {"id": 80}}],
        )
        self.assertFalse(enriched.home_europe_congestion)
        self.assertTrue(enriched.away_europe_congestion)
        self.assertTrue(metadata["away_europe_context"]["europe_sandwich"])

    def test_structured_home_and_venue_are_used(self):
        _, metadata = enrich_match_context(
            FakeProvider(), self.raw, self.context, [], []
        )
        self.assertEqual(metadata["official_home_team_id"], 1)
        self.assertEqual(metadata["official_away_team_id"], 2)
        self.assertTrue(metadata["venue_verified"])
        self.assertEqual(metadata["venue_name"], "Example Stadium")

    def test_rotation_reduces_away_attack_factor(self):
        enriched, metadata = enrich_match_context(
            FakeProvider(),
            self.raw,
            self.context,
            home_history=[{"fixture": {"id": 80}}],
            away_history=[{"fixture": {"id": 80}}],
        )
        self.assertLess(enriched.lineup_attack_factor_away, 1.0)
        self.assertTrue(metadata["away_lineup_context"]["lineup_available"])


if __name__ == "__main__":
    unittest.main()
