from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from engine.batch_features import BatchFeatureEngineeringService
from engine.features import FeatureEngineeringService
from engine.models import Fixture, Prediction, Team
from scanner.ingestion import DataIngestionService
from scanner.management.commands.scan_daily import Command as ScanDailyCommand
from scanner.management.commands.score_v8 import Command as ScoreV8Command


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

    def test_prediction_change_detection_uses_database_decimal_precision(self):
        evaluation = {
            "probability": 0.61234549,
            "fair_odds": 1.63349,
            "market_odds": 1.91049,
            "edge": 0.088884,
            "expected_value": 0.169994,
            "score": 82.126,
            "tier": "TIER_A",
            "reasons": {"v8_gate_failures": []},
        }
        defaults = ScoreV8Command._prediction_defaults(evaluation)
        pred = Prediction(
            probability=Decimal("0.61235"),
            fair_odds=Decimal("1.633"),
            market_odds=Decimal("1.910"),
            edge=Decimal("0.08888"),
            expected_value=Decimal("0.16999"),
            score=Decimal("82.13"),
            tier="TIER_A",
            reasons={"v8_gate_failures": []},
        )

        self.assertFalse(ScoreV8Command._prediction_changed(pred, defaults))
        pred.score = Decimal("82.12")
        self.assertTrue(ScoreV8Command._prediction_changed(pred, defaults))

    def test_bootstrap_bulk_persistence_is_bounded_and_exact(self):
        home = Team.objects.create(external_id="bulk-home", name="Bulk Home")
        away = Team.objects.create(external_id="bulk-away", name="Bulk Away")
        base = datetime(2026, 8, 16, 12, 0, tzinfo=dt_timezone.utc)
        fixtures = [
            Fixture.objects.create(
                external_id=f"bulk-{i}", competition="Test League", kickoff=base + timedelta(minutes=i),
                home_team=home, away_team=away, status="NS",
            )
            for i in range(40)
        ]
        btts = {
            "market": "BTTS", "selection": "YES", "probability": 0.651234,
            "fair_odds": 1.535, "market_odds": 1.82, "edge": 0.10123,
            "expected_value": 0.1856, "score": 84.126, "tier": "TIER_A",
            "reasons": {"v8_gates_passed": True},
        }
        over = {
            "market": "OVER_2_5", "selection": "OVER", "probability": 0.691234,
            "fair_odds": 1.447, "market_odds": 1.75, "edge": 0.11987,
            "expected_value": 0.2091, "score": 86.334, "tier": "TIER_A",
            "reasons": {"v8_gates_passed": True},
        }
        rows = [(fixture, evaluation) for fixture in fixtures for evaluation in (btts, over)]

        with CaptureQueriesContext(connection) as captured:
            created, updated, unchanged = ScanDailyCommand._bulk_persist_evaluations(rows)

        self.assertEqual((created, updated, unchanged), (80, 0, 0))
        self.assertEqual(Prediction.objects.count(), 80)
        self.assertLessEqual(len(captured), 6)
        sample = Prediction.objects.get(fixture=fixtures[0], market="OVER_2_5", selection="OVER")
        self.assertEqual(sample.probability, Decimal("0.69123"))
        self.assertEqual(sample.market_odds, Decimal("1.750"))
        self.assertEqual(sample.expected_value, Decimal("0.20910"))
        self.assertEqual(sample.score, Decimal("86.33"))

        with CaptureQueriesContext(connection) as captured_again:
            created, updated, unchanged = ScanDailyCommand._bulk_persist_evaluations(rows)
        self.assertEqual((created, updated, unchanged), (0, 0, 80))
        self.assertLessEqual(len(captured_again), 4)
