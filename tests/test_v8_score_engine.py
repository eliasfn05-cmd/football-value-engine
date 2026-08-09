from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from engine.features import FeatureVector, VenueProfile
from engine.models import Fixture, Prediction, Team
from engine.score_v8 import ScoreEngineV8, V8_MODEL_VERSION


class ScoreEngineV8Tests(TestCase):
    def setUp(self):
        self.home = Team.objects.create(external_id="v8-home", name="V8 Home")
        self.away = Team.objects.create(external_id="v8-away", name="V8 Away")
        self.fixture = Fixture.objects.create(
            external_id="v8-target",
            competition="Test League",
            round="Regular Season - 8",
            kickoff=timezone.now() + timedelta(days=1),
            home_team=self.home,
            away_team=self.away,
            status="NS",
        )
        self.engine = ScoreEngineV8()

    @staticmethod
    def _profile(sample_size=5):
        return VenueProfile(
            sample_size=sample_size,
            goals_for=1.9,
            goals_against=1.4,
            over25_rate=0.8,
            btts_rate=0.8,
            clean_sheet_rate=0.1,
            failed_to_score_rate=0.1,
            btts_over25_escalation_rate=0.8,
            low_score_rate=0.2,
            one_one_rate=0.1,
        )

    def _features(self, *, sample_size=5, quality=100.0, home_lineup=0.9, away_lineup=0.9):
        home = self._profile(sample_size)
        away = self._profile(sample_size)
        return FeatureVector(
            fixture_id=self.fixture.external_id,
            home_team=self.home.name,
            away_team=self.away.name,
            home_profile=home,
            away_profile=away,
            home_over25_last5_home=home.over25_rate,
            away_over25_last5_away=away.over25_rate,
            home_btts_last5_home=home.btts_rate,
            away_btts_last5_away=away.btts_rate,
            home_clean_sheet_rate=home.clean_sheet_rate,
            away_clean_sheet_rate=away.clean_sheet_rate,
            home_failed_to_score_rate=home.failed_to_score_rate,
            away_failed_to_score_rate=away.failed_to_score_rate,
            home_table_position=3,
            away_table_position=5,
            home_points_per_game=2.0,
            away_points_per_game=1.7,
            home_lineup_continuity=home_lineup,
            away_lineup_continuity=away_lineup,
            btts_market_odds=2.0,
            over25_market_odds=2.0,
            data_quality_score=quality,
        )

    def test_small_sample_is_penalized_not_blocked(self):
        result = self.engine.evaluate(self.fixture, self._features(sample_size=2, quality=55.0))
        for evaluation in result.values():
            reasons = evaluation["reasons"]
            self.assertNotIn("no_home_venue_history", reasons["v8_gate_failures"])
            self.assertNotIn("no_away_venue_history", reasons["v8_gate_failures"])
            self.assertGreater(reasons["evidence_penalty"], 0)
            self.assertIn("home_venue_sample_soft_penalty", reasons["v8_soft_warnings"])
            self.assertIn("away_venue_sample_soft_penalty", reasons["v8_soft_warnings"])

    def test_zero_venue_history_remains_hard_block(self):
        result = self.engine.evaluate(self.fixture, self._features(sample_size=0, quality=20.0))
        for evaluation in result.values():
            self.assertEqual(evaluation["tier"], "")
            self.assertFalse(evaluation["reasons"]["v8_gates_passed"])
            self.assertIn("no_home_venue_history", evaluation["reasons"]["v8_gate_failures"])
            self.assertIn("no_away_venue_history", evaluation["reasons"]["v8_gate_failures"])

    def test_heavy_rotation_reduces_attack_factor(self):
        features = self._features(home_lineup=0.45, away_lineup=0.90)
        context, audit = self.engine._context_from_features(self.fixture, features)
        self.assertEqual(context.lineup_attack_factor_home, 0.90)
        self.assertEqual(context.lineup_attack_factor_away, 1.0)
        self.assertEqual(audit["home_lineup_state"], "heavy_rotation")
        self.assertEqual(audit["away_lineup_state"], "stable_lineup")

    def test_recalculation_updates_instead_of_duplicating_predictions(self):
        features = self._features()
        self.engine.evaluate_and_persist(self.fixture, features)
        self.engine.evaluate_and_persist(self.fixture, features)
        rows = Prediction.objects.filter(fixture=self.fixture, model_version=V8_MODEL_VERSION)
        self.assertEqual(rows.count(), 2)
        self.assertEqual(set(rows.values_list("market", flat=True)), {"BTTS", "OVER_2_5"})
