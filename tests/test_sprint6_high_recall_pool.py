from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from engine.candidate_pool import CandidatePoolRule, high_recall_candidate_pool
from engine.models import Competition, Fixture, Prediction, Team
from engine.score_v8 import V8_MODEL_VERSION


class HighRecallCandidatePoolTests(TestCase):
    def setUp(self):
        self.home = Team.objects.create(external_id="hr-home", name="High Recall Home")
        self.away = Team.objects.create(external_id="hr-away", name="High Recall Away")
        self.official = Competition.objects.create(
            external_id="hr-league",
            name="Official First Division",
            country="Belgium",
            competition_type="League",
        )
        self.friendly = Competition.objects.create(
            external_id="hr-friendly",
            name="Friendlies Clubs",
            country="World",
            competition_type="Friendly",
        )
        self.target_date = timezone.localdate()

    def _fixture(self, external_id: str, competition: Competition):
        return Fixture.objects.create(
            external_id=external_id,
            competition=competition.name,
            competition_ref=competition,
            kickoff=timezone.now() + timedelta(hours=4),
            home_team=self.home,
            away_team=self.away,
            status="NS",
        )

    def _prediction(self, fixture, *, score, probability=.65, edge=None, ev=None):
        return Prediction.objects.create(
            fixture=fixture,
            model_version=V8_MODEL_VERSION,
            market="OVER_2_5",
            selection="OVER",
            probability=Decimal(str(probability)),
            fair_odds=Decimal("1.54"),
            market_odds=Decimal("1.83"),
            edge=Decimal(str(edge)) if edge is not None else None,
            expected_value=Decimal(str(ev)) if ev is not None else None,
            score=Decimal(str(score)),
            tier="",
            reasons={"data_quality_score": 80.0},
        )

    def test_score_89_candidate_survives_preselection(self):
        fixture = self._fixture("westerlo-like", self.official)
        self._prediction(fixture, score=89, edge=.10, ev=.18)

        pool = high_recall_candidate_pool(self.target_date)

        self.assertIn(fixture.id, [entry.fixture_id for entry in pool])
        entry = next(item for item in pool if item.fixture_id == fixture.id)
        self.assertIn("score", entry.entry_reasons)
        self.assertIn("edge", entry.entry_reasons)
        self.assertIn("ev", entry.entry_reasons)

    def test_high_ev_candidate_can_enter_even_below_score_floor(self):
        fixture = self._fixture("ev-route", self.official)
        self._prediction(fixture, score=76, edge=.04, ev=.16)

        pool = high_recall_candidate_pool(self.target_date)

        entry = next(item for item in pool if item.fixture_id == fixture.id)
        self.assertEqual(entry.entry_reasons, ("ev",))

    def test_friendly_is_excluded_even_with_elite_metrics(self):
        fixture = self._fixture("friendly-elite", self.friendly)
        self._prediction(fixture, score=100, probability=.80, edge=.30, ev=.60)

        pool = high_recall_candidate_pool(self.target_date)

        self.assertNotIn(fixture.id, [entry.fixture_id for entry in pool])

    def test_limit_is_applied_after_fixture_deduplication(self):
        for index in range(5):
            home = Team.objects.create(external_id=f"h-{index}", name=f"Home {index}")
            away = Team.objects.create(external_id=f"a-{index}", name=f"Away {index}")
            fixture = Fixture.objects.create(
                external_id=f"fixture-{index}",
                competition=self.official.name,
                competition_ref=self.official,
                kickoff=timezone.now() + timedelta(hours=4, minutes=index),
                home_team=home,
                away_team=away,
                status="NS",
            )
            self._prediction(fixture, score=90 - index, edge=.08, ev=.12)

        pool = high_recall_candidate_pool(
            self.target_date,
            rule=CandidatePoolRule(limit=3),
        )
        self.assertEqual(len(pool), 3)
