from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from scanner.ingestion import DataIngestionService


class FastIngestionTests(SimpleTestCase):
    def test_fixtures_only_skips_lineups_statistics_and_standings(self):
        provider = Mock()
        provider.fixtures_by_date.return_value = [{"fixture": {"id": 123}}]
        service = DataIngestionService(provider)

        fake_fixture = SimpleNamespace(
            external_id="123",
            status="NS",
            competition_ref=SimpleNamespace(id=9, external_id="39"),
        )

        with patch.object(service, "upsert_fixture", return_value=fake_fixture):
            report = service.ingest_date(date(2026, 8, 8), include_details=False)

        provider.fixtures_by_date.assert_called_once_with(date(2026, 8, 8))
        provider.fixture_lineups.assert_not_called()
        provider.fixture_statistics.assert_not_called()
        provider.standings.assert_not_called()
        self.assertEqual(report["fixtures"], 1)
        self.assertEqual(report["lineups_created"], 0)
        self.assertEqual(report["statistics_created"], 0)
        self.assertEqual(report["standings_created"], 0)
        self.assertEqual(report["errors"], [])
