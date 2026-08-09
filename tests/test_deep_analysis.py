from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from engine.deep_analysis import DEEP_ANALYSIS_VERSION, DeepMatchAnalysisService
from engine.models import Fixture, Prediction, Team
from engine.score_v8 import V8_MODEL_VERSION


class DeepAnalysisTests(TestCase):
    def setUp(self):
        self.home = Team.objects.create(external_id="deep-home", name="Deep Home")
        self.away = Team.objects.create(external_id="deep-away", name="Deep Away")
        self.target = Fixture.objects.create(
            external_id="deep-target",
            competition="Official League",
            kickoff=timezone.now() + timedelta(days=1),
            home_team=self.home,
            away_team=self.away,
            status="NS",
        )

    def _history(self, *, team, venue, overs):
        for index, is_over in enumerate(overs):
            opponent = Team.objects.create(
                external_id=f"opp-{team.id}-{venue}-{index}",
                name=f"Opponent {team.id}-{venue}-{index}",
            )
            if is_over:
                gf, ga = 2, 1
            else:
                gf, ga = 1, 1
            kwargs = {
                "external_id": f"hist-{team.id}-{venue}-{index}",
                "competition": "Official League",
                "kickoff": self.target.kickoff - timedelta(days=index + 2),
                "status": "FT",
            }
            if venue == "home":
                kwargs.update(home_team=team, away_team=opponent, home_goals=gf, away_goals=ga)
            else:
                kwargs.update(home_team=opponent, away_team=team, home_goals=ga, away_goals=gf)
            Fixture.objects.create(**kwargs)

    def _prediction(self, market, score=94, ev=.20, edge=.12, probability=.70):
        return Prediction.objects.create(
            fixture=self.target,
            model_version=V8_MODEL_VERSION,
            market=market,
            selection="OVER" if market == "OVER_2_5" else "YES",
            probability=Decimal(str(probability)),
            fair_odds=Decimal("1.50"),
            market_odds=Decimal("2.00"),
            edge=Decimal(str(edge)),
            expected_value=Decimal(str(ev)),
            score=Decimal(str(score)),
            tier="",
            reasons={"v8_gates_passed": True},
        )

    def test_low_home_over_rate_penalizes_over_and_records_last10_evidence(self):
        self._history(team=self.home, venue="home", overs=[True, True, False, False, False, False, False, False, False, False])
        self._history(team=self.away, venue="away", overs=[True, True, True, True, True, True, False, False, False, False])
        over = self._prediction("OVER_2_5")
        self._prediction("BTTS", score=88, ev=.10, edge=.08, probability=.64)

        DeepMatchAnalysisService().analyze_fixture(self.target)
        over.refresh_from_db()
        reasons = over.reasons
        evidence = reasons["deep_analysis_evidence"]
        canonical = reasons["deep_analysis"]
        self.assertEqual(reasons["deep_analysis_version"], DEEP_ANALYSIS_VERSION)
        self.assertEqual(evidence["home_sample"], 10)
        self.assertEqual(evidence["home_over25_rate"], 0.2)
        self.assertEqual(canonical["status"], "complete")
        self.assertEqual(canonical["home_n"], 10)
        self.assertEqual(canonical["home_over25"], 0.2)
        self.assertEqual(reasons["deep_home_n"], 10)
        self.assertEqual(reasons["deep_home_over25"], 0.2)
        self.assertIn("home_over25_deep_low", reasons["deep_analysis_warnings"])
        self.assertLess(float(over.score), 94.0)
        self.assertEqual(float(over.score), float(canonical["score"]))

    def test_only_one_market_is_marked_preferred(self):
        self._history(team=self.home, venue="home", overs=[True] * 8 + [False] * 2)
        self._history(team=self.away, venue="away", overs=[True] * 8 + [False] * 2)
        self._prediction("OVER_2_5", score=92, ev=.18, edge=.12, probability=.70)
        self._prediction("BTTS", score=89, ev=.10, edge=.07, probability=.64)

        rows = DeepMatchAnalysisService().analyze_fixture(self.target)
        preferred = [row for row in rows if (row.reasons or {}).get("deep_preferred_market")]
        self.assertEqual(len(preferred), 1)
        self.assertTrue(preferred[0].reasons["deep_analysis"]["preferred_market"])

    def test_refresh_is_idempotent_and_does_not_compound_deep_score(self):
        self._history(team=self.home, venue="home", overs=[True] * 6 + [False] * 4)
        self._history(team=self.away, venue="away", overs=[True] * 6 + [False] * 4)
        over = self._prediction("OVER_2_5", score=94, ev=.20, edge=.12, probability=.70)
        self._prediction("BTTS", score=88, ev=.10, edge=.08, probability=.64)
        service = DeepMatchAnalysisService()

        service.analyze_fixture(self.target)
        over.refresh_from_db()
        first_score = float(over.score)
        original_v8 = over.reasons["score_before_deep_analysis"]

        service.analyze_fixture(self.target)
        over.refresh_from_db()
        second_score = float(over.score)

        self.assertEqual(first_score, second_score)
        self.assertEqual(original_v8, 94.0)
        self.assertEqual(over.reasons["score_before_deep_analysis"], 94.0)
