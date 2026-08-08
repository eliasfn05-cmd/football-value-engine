from datetime import date, datetime, timedelta, timezone as dt_timezone

from django.test import TestCase

from engine.batch_features import BatchFeatureEngineeringService
from engine.features import FeatureEngineeringService
from engine.models import Fixture, Team
from scanner.ingestion import DataIngestionService


class FakeProvider:
    def __init__(self, fixtures):
        self.fixtures = fixtures
        self.lineup_calls = 0
        self.statistics_calls = 0
        self.standings_calls = 0

    def fixtures_by_date(self, target_date):
        return self.fixtures

    def fixture_lineups(self, fixture_id):
        self.lineup_calls += 1
        return []

    def fixture_statistics(self, fixture_id):
        self.statistics_calls += 1
        return []

    def standings(self, league_id, season):
        self.standings_calls += 1
        return []


class BatchDailyPathTests(TestCase):
    def _raw_fixture(self, fixture_id=1001):
        return {
            "fixture": {
                "id": fixture_id,
                "date": "2026-08-08T12:00:00-05:00",
                "status": {"short": "NS"},
                "venue": {"name": "Test", "city": "Lima"},
                "referee": None,
            },
            "league": {
                "id": 999,
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

    def test_fast_ingest_bulk_path_skips_detail_endpoints(self):
        provider = FakeProvider([self._raw_fixture()])
        report = DataIngestionService(provider).ingest_date(date(2026, 8, 8), include_details=False)

        self.assertEqual(report["fixtures"], 1)
        self.assertEqual(Fixture.objects.count(), 1)
        self.assertEqual(provider.lineup_calls, 0)
        self.assertEqual(provider.statistics_calls, 0)
        self.assertEqual(provider.standings_calls, 0)

    def test_batch_profiles_match_single_fixture_feature_builder(self):
        home = Team.objects.create(external_id="10", name="H")
        away = Team.objects.create(external_id="20", name="A")
        base = datetime(2026, 8, 8, 17, 0, tzinfo=dt_timezone.utc)

        for i in range(5):
            opponent_h = Team.objects.create(external_id=f"h{i}", name=f"HO{i}")
            opponent_a = Team.objects.create(external_id=f"a{i}", name=f"AO{i}")
            Fixture.objects.create(
                external_id=f"hh{i}", competition="T", kickoff=base - timedelta(days=i + 2),
                home_team=home, away_team=opponent_h, status="FT", home_goals=2, away_goals=1,
            )
            Fixture.objects.create(
                external_id=f"aa{i}", competition="T", kickoff=base - timedelta(days=i + 2, hours=1),
                home_team=opponent_a, away_team=away, status="FT", home_goals=1, away_goals=1,
            )

        target = Fixture.objects.create(
            external_id="target", competition="T", kickoff=base,
            home_team=home, away_team=away, status="NS",
        )

        single = FeatureEngineeringService().build(target)
        batch = BatchFeatureEngineeringService([target])
        batch.preload()
        fast = batch.build(target)

        self.assertEqual(fast.home_profile, single.home_profile)
        self.assertEqual(fast.away_profile, single.away_profile)
