from copy import deepcopy
from datetime import date
from unittest.mock import Mock

from django.test import TestCase

from scanner.ingestion import DataIngestionService


class FastIngestionTests(TestCase):
    def _fixture(self):
        return {
            "fixture": {
                "id": 123,
                "date": "2026-08-08T12:00:00-05:00",
                "status": {"short": "NS"},
                "venue": {"name": "Estadio Test", "city": "Lima"},
                "referee": None,
            },
            "league": {
                "id": 99,
                "name": "Test League",
                "country": "Peru",
                "season": 2026,
                "type": "League",
                "round": "Regular Season - 1",
                "logo": "",
            },
            "teams": {
                "home": {"id": 1, "name": "Home", "logo": ""},
                "away": {"id": 2, "name": "Away", "logo": ""},
            },
            "goals": {"home": None, "away": None},
        }

    def _service(self, payload):
        provider = Mock()
        provider.fixtures_by_date.return_value = payload
        return provider, DataIngestionService(provider)

    def test_fixtures_only_skips_detail_endpoints(self):
        provider, service = self._service([self._fixture()])
        report = service.ingest_date(date(2026, 8, 8), include_details=False)

        provider.fixtures_by_date.assert_called_once_with(date(2026, 8, 8))
        provider.fixture_lineups.assert_not_called()
        provider.fixture_statistics.assert_not_called()
        provider.standings.assert_not_called()
        self.assertEqual(report["fixtures"], 1)
        self.assertEqual(report["fixtures_created"], 1)
        self.assertEqual(report["fixtures_changed"], 0)
        self.assertEqual(report["fixtures_unchanged"], 0)
        self.assertEqual(report["errors"], [])

    def test_second_identical_fast_ingest_is_unchanged(self):
        raw = self._fixture()
        _provider, service = self._service([raw])
        first = service.ingest_date(date(2026, 8, 8), include_details=False)
        second = service.ingest_date(date(2026, 8, 8), include_details=False)

        self.assertEqual(first["fixtures_created"], 1)
        self.assertEqual(second["fixtures_created"], 0)
        self.assertEqual(second["fixtures_changed"], 0)
        self.assertEqual(second["fixtures_unchanged"], 1)

    def test_real_fixture_change_is_reported(self):
        raw = self._fixture()
        provider, service = self._service([raw])
        service.ingest_date(date(2026, 8, 8), include_details=False)

        changed = deepcopy(raw)
        changed["fixture"]["status"]["short"] = "FT"
        changed["goals"] = {"home": 2, "away": 1}
        provider.fixtures_by_date.return_value = [changed]

        report = service.ingest_date(date(2026, 8, 8), include_details=False)
        self.assertEqual(report["fixtures_created"], 0)
        self.assertEqual(report["fixtures_changed"], 1)
        self.assertEqual(report["fixtures_unchanged"], 0)
