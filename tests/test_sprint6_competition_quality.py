from datetime import datetime, time, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from engine.competition_quality import classify_competition
from engine.deep_analysis import DEEP_ANALYSIS_VERSION
from engine.models import Competition, DailyPremiumSelection, Fixture, FixtureScoreState, Prediction, Team
from engine.premium_selection import DailyPremiumSelector
from engine.score_v8 import V8_MODEL_VERSION
from scanner.management.commands.score_v8 import Command as ScoreCommand


class Sprint62CompetitionQualityTests(TestCase):
    def setUp(self):
        self.home = Team.objects.create(external_id="s62-home", name="Sprint 62 Home")
        self.away = Team.objects.create(external_id="s62-away", name="Sprint 62 Away")
        self.target_date = timezone.localdate() + timedelta(days=1)
        self.kickoff = timezone.make_aware(datetime.combine(self.target_date, time(12, 0)))

    def _fixture(self, external_id: str, competition: str) -> Fixture:
        return Fixture.objects.create(
            external_id=external_id,
            competition=competition,
            kickoff=self.kickoff,
            home_team=self.home,
            away_team=self.away,
            status="NS",
        )

    def _prediction(self, fixture: Fixture, *, score="96.00", ev="0.30000", edge="0.18000") -> Prediction:
        return Prediction.objects.create(
            fixture=fixture,
            model_version=V8_MODEL_VERSION,
            market="BTTS",
            selection="YES",
            probability=Decimal("0.70000"),
            fair_odds=Decimal("1.429"),
            market_odds=Decimal("2.000"),
            edge=Decimal(edge),
            expected_value=Decimal(ev),
            score=Decimal(score),
            tier="TIER_A",
            reasons={
                "v8_gates_passed": True,
                "data_quality_score": 90.0,
                "bookmaker": "Betano",
                "deep_analysis_version": DEEP_ANALYSIS_VERSION,
                "deep_analysis_passed": True,
                "deep_preferred_market": True,
                "deep_score": float(score),
            },
        )

    def test_classifies_friendlies_as_excluded_tier_4(self):
        fixture = self._fixture("friendly", "Club Friendlies")
        quality = classify_competition(fixture)
        self.assertTrue(quality.excluded)
        self.assertEqual(quality.level, 4)
        self.assertEqual(quality.reason, "friendly_or_exhibition")

    def test_uses_competition_ref_name_even_when_fixture_name_is_generic(self):
        ref = Competition.objects.create(
            external_id="friendly-ref",
            name="Friendlies Clubs",
            country="World",
            season=2026,
            competition_type="Cup",
        )
        fixture = Fixture.objects.create(
            external_id="generic-world-friendly",
            competition="World",
            competition_ref=ref,
            season=2026,
            round="Club Friendlies",
            kickoff=self.kickoff,
            home_team=self.home,
            away_team=self.away,
            status="NS",
        )
        quality = classify_competition(fixture)
        self.assertTrue(quality.excluded)
        self.assertEqual(quality.label, "TIER_4_EXCLUDED")

    def test_classifies_elite_official_and_development_competitions(self):
        elite = classify_competition(self._fixture("elite", "UEFA Champions League"))
        official = classify_competition(self._fixture("official", "Liga 1"))
        development = classify_competition(self._fixture("development", "U21 Premier League"))
        self.assertEqual(elite.level, 1)
        self.assertFalse(elite.excluded)
        self.assertEqual(official.level, 2)
        self.assertFalse(official.excluded)
        self.assertEqual(development.level, 3)
        self.assertFalse(development.excluded)
        self.assertLess(development.quality_score, official.quality_score)

    def test_selector_never_publishes_friendly_even_with_superior_numbers(self):
        friendly = self._fixture("friendly-pick", "International Friendly")
        official = self._fixture("official-pick", "Premier League")
        self._prediction(friendly, score="100.00", ev="0.50000", edge="0.25000")
        official_prediction = self._prediction(official, score="94.00", ev="0.20000", edge="0.12000")
        selections = DailyPremiumSelector().select(self.target_date)
        self.assertEqual(len(selections), 1)
        self.assertEqual(selections[0].prediction_id, official_prediction.id)
        self.assertEqual(DailyPremiumSelection.objects.count(), 1)

    def test_scoring_cleanup_removes_legacy_friendly_prediction_and_state(self):
        friendly = self._fixture("legacy-friendly", "Pre-Season Friendly")
        self._prediction(friendly)
        FixtureScoreState.objects.create(
            fixture=friendly,
            model_version=V8_MODEL_VERSION,
            feature_fingerprint="a" * 64,
        )
        excluded = ScoreCommand._remove_excluded_fixture_state([friendly])
        self.assertEqual(excluded, 1)
        self.assertFalse(Prediction.objects.filter(fixture=friendly, model_version=V8_MODEL_VERSION).exists())
        self.assertFalse(FixtureScoreState.objects.filter(fixture=friendly, model_version=V8_MODEL_VERSION).exists())
