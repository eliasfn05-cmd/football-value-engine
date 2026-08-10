from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from engine.deep_analysis import DEEP_ANALYSIS_VERSION, DeepMatchAnalysisService
from engine.models import Fixture, Prediction, Team
from engine.score_v8 import V8_MODEL_VERSION


class DeepAnalysisRecencyTests(TestCase):
    def setUp(self):
        self.home = Team.objects.create(external_id="recent-home", name="Recent Home")
        self.away = Team.objects.create(external_id="recent-away", name="Recent Away")
        self.target = Fixture.objects.create(
            external_id="recent-target",
            competition="Official League",
            kickoff=timezone.now() + timedelta(days=1),
            home_team=self.home,
            away_team=self.away,
            status="NS",
        )

    def _history(self, *, team, venue, overs):
        for index, is_over in enumerate(overs):
            opponent = Team.objects.create(
                external_id=f"recent-opp-{team.id}-{venue}-{index}",
                name=f"Recent Opponent {team.id}-{venue}-{index}",
            )
            gf, ga = (2, 1) if is_over else (1, 1)
            kwargs = {
                "external_id": f"recent-hist-{team.id}-{venue}-{index}",
                "competition": "Official League",
                "kickoff": self.target.kickoff - timedelta(days=index + 2),
                "status": "FT",
            }
            if venue == "home":
                kwargs.update(home_team=team, away_team=opponent, home_goals=gf, away_goals=ga)
            else:
                kwargs.update(home_team=opponent, away_team=team, home_goals=ga, away_goals=gf)
            Fixture.objects.create(**kwargs)

    def _prediction(self, market, score=96, ev=.20, edge=.12, probability=.70):
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

    def test_last_five_over_drought_is_visible_and_penalized(self):
        # Long-term venue rate is acceptable (5/10), but the five most recent
        # away matches are 0/5 Over 2.5. Deep analysis must not treat that as stable.
        self._history(team=self.home, venue="home", overs=[True] * 7 + [False] * 3)
        self._history(team=self.away, venue="away", overs=[False] * 5 + [True] * 5)
        over = self._prediction("OVER_2_5", score=98, ev=.22, edge=.13, probability=.71)
        self._prediction("BTTS", score=88, ev=.08, edge=.06, probability=.62)

        DeepMatchAnalysisService().analyze_fixture(self.target)
        over.refresh_from_db()
        evidence = over.reasons["deep_analysis_evidence"]

        self.assertEqual(over.reasons["deep_analysis_version"], DEEP_ANALYSIS_VERSION)
        self.assertEqual(evidence["away_over25_rate"], 0.5)
        self.assertEqual(evidence["away_recent_n"], 5)
        self.assertEqual(evidence["away_recent_over25_rate"], 0.0)
        self.assertGreater(evidence["recency_penalty"], 0.0)
        self.assertIn("away_over25_recent_drought", over.reasons["deep_analysis_warnings"])
        self.assertLess(float(over.score), 90.0)

    def test_strong_recent_over_form_has_no_recency_penalty(self):
        self._history(team=self.home, venue="home", overs=[True] * 8 + [False] * 2)
        self._history(team=self.away, venue="away", overs=[True] * 7 + [False] * 3)
        over = self._prediction("OVER_2_5", score=98, ev=.20, edge=.12, probability=.72)
        self._prediction("BTTS", score=88, ev=.08, edge=.06, probability=.62)

        DeepMatchAnalysisService().analyze_fixture(self.target)
        over.refresh_from_db()
        evidence = over.reasons["deep_analysis_evidence"]

        self.assertEqual(evidence["home_recent_over25_rate"], 1.0)
        self.assertEqual(evidence["away_recent_over25_rate"], 1.0)
        self.assertEqual(evidence["recency_penalty"], 0.0)
        self.assertNotIn("home_over25_recent_drought", over.reasons["deep_analysis_warnings"])
        self.assertNotIn("away_over25_recent_drought", over.reasons["deep_analysis_warnings"])

    def test_away_recent_btts_uses_only_matches_played_away(self):
        """Sprint 7.9 regression: general form must never leak into away BTTS.

        The away team has five very recent HOME matches with BTTS=5/5, but its
        five most recent AWAY matches have BTTS=0/5. The Deep evidence consumed
        by Premium must therefore report away_recent_btts_rate=0.0, not 0.5/1.0.
        """
        # Give the target home team enough legitimate home history.
        self._history(team=self.home, venue="home", overs=[True] * 10)

        # Visitor's five most recent AWAY matches: zero BTTS (0-1 from visitor
        # perspective; opponent scores, visitor fails to score).
        for index in range(5):
            opponent = Team.objects.create(
                external_id=f"away-only-opp-{index}",
                name=f"Away Only Opponent {index}",
            )
            Fixture.objects.create(
                external_id=f"away-only-{index}",
                competition="Official League",
                kickoff=self.target.kickoff - timedelta(days=index + 2),
                home_team=opponent,
                away_team=self.away,
                home_goals=1,
                away_goals=0,
                status="FT",
            )

        # Even more recent GENERAL-form noise: five HOME matches with BTTS=5/5.
        # These must be invisible to venue_profile(self.away, 'away', ...).
        for index in range(5):
            opponent = Team.objects.create(
                external_id=f"general-noise-opp-{index}",
                name=f"General Noise Opponent {index}",
            )
            Fixture.objects.create(
                external_id=f"general-noise-{index}",
                competition="Official League",
                kickoff=self.target.kickoff - timedelta(hours=index + 2),
                home_team=self.away,
                away_team=opponent,
                home_goals=2,
                away_goals=1,
                status="FT",
            )

        btts = self._prediction("BTTS", score=98, ev=.20, edge=.12, probability=.72)
        self._prediction("OVER_2_5", score=88, ev=.08, edge=.06, probability=.62)

        service = DeepMatchAnalysisService()
        away_profile = service.venue_profile(self.away, "away", self.target)
        self.assertEqual(away_profile.recent_sample_size, 5)
        self.assertEqual(away_profile.recent_btts_rate, 0.0)
        self.assertEqual(away_profile.recent_failed_to_score_rate, 1.0)

        service.analyze_fixture(self.target)
        btts.refresh_from_db()
        evidence = btts.reasons["deep_analysis_evidence"]

        self.assertEqual(evidence["away_recent_n"], 5)
        self.assertEqual(evidence["away_recent_btts_rate"], 0.0)
        self.assertEqual(evidence["away_recent_failed_to_score_rate"], 1.0)
        self.assertIn("away_btts_recent_drought", btts.reasons["deep_analysis_warnings"])
