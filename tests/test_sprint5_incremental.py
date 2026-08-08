from datetime import datetime, timezone as dt_timezone

from django.db import IntegrityError, transaction
from django.test import TestCase

from engine.features import FeatureVector, VenueProfile
from engine.models import Fixture, FixtureScoreState, Team
from engine.score_v8 import V8_MODEL_VERSION
from scanner.management.commands.score_v8 import Command


class Sprint5IncrementalTests(TestCase):
    def setUp(self):
        self.home = Team.objects.create(external_id="s5-home", name="Sprint Home")
        self.away = Team.objects.create(external_id="s5-away", name="Sprint Away")
        self.fixture = Fixture.objects.create(
            external_id="s5-fixture",
            competition="Sprint League",
            season=2026,
            round="Round 1",
            kickoff=datetime(2026, 8, 8, 17, 0, tzinfo=dt_timezone.utc),
            home_team=self.home,
            away_team=self.away,
            status="NS",
        )

    def _features(self, *, odds=2.0):
        profile = VenueProfile(5, 1.6, 1.0, 0.6, 0.6, 0.4, 0.2)
        return FeatureVector(
            fixture_id=self.fixture.external_id,
            home_team=self.home.name,
            away_team=self.away.name,
            home_profile=profile,
            away_profile=profile,
            home_over25_last5_home=0.6,
            away_over25_last5_away=0.6,
            home_btts_last5_home=0.6,
            away_btts_last5_away=0.6,
            home_clean_sheet_rate=0.4,
            away_clean_sheet_rate=0.4,
            home_failed_to_score_rate=0.2,
            away_failed_to_score_rate=0.2,
            home_table_position=2,
            away_table_position=5,
            home_points_per_game=2.0,
            away_points_per_game=1.4,
            home_lineup_continuity=0.82,
            away_lineup_continuity=0.73,
            btts_market_odds=odds,
            over25_market_odds=1.95,
            data_quality_score=100.0,
        )

    def test_feature_fingerprint_is_stable_and_changes_with_inputs(self):
        first = Command._feature_fingerprint(self.fixture, self._features())
        second = Command._feature_fingerprint(self.fixture, self._features())
        changed = Command._feature_fingerprint(self.fixture, self._features(odds=2.1))

        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, changed)

    def test_fixture_model_checkpoint_is_unique(self):
        FixtureScoreState.objects.create(
            fixture=self.fixture,
            model_version=V8_MODEL_VERSION,
            feature_fingerprint="a" * 64,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                FixtureScoreState.objects.create(
                    fixture=self.fixture,
                    model_version=V8_MODEL_VERSION,
                    feature_fingerprint="b" * 64,
                )
