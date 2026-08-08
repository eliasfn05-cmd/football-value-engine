from datetime import date
from unittest.mock import patch

from django.test import SimpleTestCase

from scanner.providers.api_football import APIFootballProvider


class APIFootballFriendlyFilterTests(SimpleTestCase):
    def setUp(self):
        self.provider = APIFootballProvider(api_key="test-key")
        self.friendly = {
            "fixture": {"id": 1, "status": {"long": "Not Started"}},
            "league": {"id": 667, "name": "Friendlies Clubs", "country": "World", "round": "Club Friendlies"},
        }
        self.official = {
            "fixture": {"id": 2, "status": {"long": "Not Started"}},
            "league": {"id": 61, "name": "Ligue 1", "country": "France", "round": "Regular Season - 1"},
        }

    def test_fixtures_by_date_excludes_friendlies_at_source(self):
        with patch.object(self.provider, "_get", return_value=[self.friendly, self.official]):
            rows = self.provider.fixtures_by_date(date(2026, 8, 8))

        self.assertEqual(rows, [self.official])
        self.assertEqual(self.provider.last_request_meta["friendlies_excluded"], 1)

    def test_historical_team_fixtures_also_exclude_friendlies(self):
        with patch.object(self.provider, "_get", return_value=[self.friendly, self.official]):
            rows = self.provider.team_recent_fixtures("33", last=20)

        self.assertEqual(rows, [self.official])

    def test_international_and_preseason_friendlies_are_detected(self):
        international = {"fixture": {}, "league": {"name": "International Friendlies", "country": "World"}}
        preseason = {"fixture": {}, "league": {"name": "World", "round": "Pre-Season Friendly"}}
        self.assertTrue(self.provider.is_friendly_fixture(international))
        self.assertTrue(self.provider.is_friendly_fixture(preseason))
