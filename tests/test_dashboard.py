import json
import os
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from backtesting.models import PredictionOutcome
from dashboard.pipeline_trigger import TriggerResult
from engine.deep_analysis import DEEP_ANALYSIS_VERSION
from engine.models import DailyPremiumSelection, Fixture, Prediction, Team
from engine.score_v8 import V8_MODEL_VERSION
from scanner.models import PipelineRun, PipelineStageRun, PremiumGenerationJob


class DashboardTests(TestCase):
    def setUp(self):
        self.home = Team.objects.create(external_id="dash-home", name="Dashboard Home")
        self.away = Team.objects.create(external_id="dash-away", name="Dashboard Away")

    def _prediction(self, *, kickoff, tier="", market="OVER_2_5", odds="1.80"):
        fixture = Fixture.objects.create(
            external_id=f"fixture-{Fixture.objects.count()+1}",
            competition="Dashboard League",
            kickoff=kickoff,
            home_team=self.home,
            away_team=self.away,
            status="NS",
        )
        deep = {
            "version": DEEP_ANALYSIS_VERSION,
            "status": "complete",
            "passed": True,
            "preferred_market": True,
            "score": 92.0,
            "v8_score": 94.0,
            "warnings": [],
            "failures": [],
            "home_n": 10,
            "away_n": 10,
            "home_over25": 0.6,
            "away_over25": 0.6,
            "home_btts": 0.6,
            "away_btts": 0.6,
            "evidence": {},
        }
        return Prediction.objects.create(
            fixture=fixture,
            model_version=V8_MODEL_VERSION,
            market=market,
            selection="OVER" if market == "OVER_2_5" else "YES",
            probability=Decimal("0.70000"),
            fair_odds=Decimal("1.429"),
            market_odds=Decimal(odds),
            edge=Decimal("0.14444"),
            expected_value=Decimal("0.26000"),
            score=Decimal("92.00"),
            tier=tier,
            reasons={
                "v8_gates_passed": True,
                "data_quality_score": 85.0,
                "bookmaker": "Betano",
                "market_confidence_passed": True,
                "market_intelligence_passed": True,
                "deep_analysis": deep,
                "deep_analysis_version": DEEP_ANALYSIS_VERSION,
                "deep_analysis_status": "complete",
                "deep_analysis_passed": True,
                "deep_preferred_market": True,
                "deep_score": 92.0,
                "score_before_deep_analysis": 94.0,
            },
        )

    def _select(self, prediction, *, rank=1, premium_tier="A"):
        return DailyPremiumSelection.objects.create(
            target_date=timezone.localdate(prediction.fixture.kickoff),
            prediction=prediction,
            rank=rank,
            premium_tier=premium_tier,
            premium_rank_score=Decimal("91.50"),
            model_version=V8_MODEL_VERSION,
            rationale={"test": True},
        )

    def test_health_endpoint_is_preserved(self):
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_dashboard_renders_no_bet_without_premium(self):
        response = self.client.get("/dashboard/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Premium Picks")
        self.assertContains(response, "NO BET")
        self.assertNotContains(response, "Candidatos cercanos a Premium")
        self.assertNotContains(response, "DIAGNÓSTICO")

    def test_dashboard_shows_ranked_future_premium_card(self):
        prediction = self._prediction(kickoff=timezone.now() + timedelta(hours=4))
        self._select(prediction, premium_tier="A")
        response = self.client.get("/dashboard/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Top Premium del día")
        self.assertContains(response, "PREMIUM A · #1")
        self.assertContains(response, "Dashboard Home")
        self.assertContains(response, "Dashboard Away")
        self.assertContains(response, "OVER_2_5")
        self.assertContains(response, "Copiar Picks")
        self.assertNotContains(response, "NO BET")

    def test_dashboard_hides_selection_without_completed_deep_analysis(self):
        prediction = self._prediction(kickoff=timezone.now() + timedelta(hours=4))
        prediction.reasons = {"v8_gates_passed": True}
        prediction.save(update_fields=["reasons"])
        self._select(prediction, premium_tier="A")
        response = self.client.get("/dashboard/")
        self.assertContains(response, "NO BET")
        self.assertNotContains(response, "PREMIUM A · #1")

    def test_near_premium_is_hidden_operationally_and_visible_in_developer(self):
        prediction = self._prediction(kickoff=timezone.now() + timedelta(hours=3), tier="", market="BTTS", odds="1.70")
        prediction.edge = Decimal("0.03000")
        prediction.expected_value = Decimal("0.05000")
        prediction.save(update_fields=["edge", "expected_value"])

        operational = self.client.get("/dashboard/")
        self.assertEqual(operational.status_code, 200)
        self.assertNotContains(operational, "Candidatos cercanos a Premium")

        developer = self.client.get("/developer/")
        self.assertEqual(developer.status_code, 200)
        self.assertContains(developer, "Developer Diagnostics")
        self.assertContains(developer, "Candidatos cercanos a Premium")
        self.assertContains(developer, "Edge &lt; 5%")

    def test_dashboard_metrics_use_only_operational_selections(self):
        prediction = self._prediction(kickoff=timezone.now() - timedelta(days=1))
        self._select(prediction, premium_tier="B")
        prediction.fixture.status = "FT"
        prediction.fixture.home_goals = 2
        prediction.fixture.away_goals = 1
        prediction.fixture.save(update_fields=["status", "home_goals", "away_goals"])
        PredictionOutcome.objects.create(
            prediction=prediction,
            result=PredictionOutcome.RESULT_WIN,
            home_goals=2,
            away_goals=1,
            stake_units=Decimal("1.000"),
            profit_units=Decimal("0.8000"),
            settled_at=timezone.now(),
            settlement_reason="over_2_5",
        )

        response = self.client.get("/dashboard/")
        self.assertContains(response, "100,0%")
        self.assertContains(response, "0,80 u")
        self.assertContains(response, "WIN")

    def test_generate_premium_rejects_wrong_pin(self):
        with patch.dict(os.environ, {"PIPELINE_TRIGGER_PIN": "2468", "GITHUB_ACTIONS_TOKEN": "token"}, clear=False):
            response = self.client.post(
                "/dashboard/generate-premium/",
                data=json.dumps({"pin": "0000"}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(PremiumGenerationJob.objects.count(), 0)

    def test_generate_premium_creates_job_and_dispatches_it(self):
        with (
            patch.dict(os.environ, {"PIPELINE_TRIGGER_PIN": "2468", "GITHUB_ACTIONS_TOKEN": "token"}, clear=False),
            patch("dashboard.views.GitHubPipelineTrigger.dispatch", return_value=TriggerResult(True, "enviado")) as dispatch,
        ):
            response = self.client.post(
                "/dashboard/generate-premium/",
                data=json.dumps({"pin": "2468"}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        job = PremiumGenerationJob.objects.get(pk=response.json()["job_id"])
        self.assertEqual(job.status, PremiumGenerationJob.STATUS_DISPATCHED)
        dispatch.assert_called_once_with(
            target_date=timezone.localdate(),
            mode="full",
            generation_job_id=job.id,
        )

    def test_generate_premium_reuses_active_job(self):
        job = PremiumGenerationJob.objects.create(
            target_date=timezone.localdate(),
            status=PremiumGenerationJob.STATUS_RUNNING,
            current_stage="SCORE_V8",
            progress_pct=35,
        )
        with patch.dict(os.environ, {"PIPELINE_TRIGGER_PIN": "2468", "GITHUB_ACTIONS_TOKEN": "token"}, clear=False):
            response = self.client.post(
                "/dashboard/generate-premium/",
                data=json.dumps({"pin": "2468"}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["already_running"])
        self.assertEqual(response.json()["job_id"], job.id)

    def test_generation_status_reports_specific_job_and_stages(self):
        run = PipelineRun.objects.create(target_date=timezone.localdate(), metadata={"model_version": V8_MODEL_VERSION})
        job = PremiumGenerationJob.objects.create(
            target_date=timezone.localdate(),
            status=PremiumGenerationJob.STATUS_RUNNING,
            pipeline=run,
            current_stage="SCORE_V8",
            progress_pct=34,
            message="Ejecutando SCORE_V8…",
        )
        PipelineStageRun.objects.create(
            pipeline=run,
            name="INGEST",
            status=PipelineStageRun.STATUS_SUCCESS,
            finished_at=timezone.now(),
            duration_seconds=4,
            records_processed=1200,
            message="ok",
        )
        response = self.client.get(f"/dashboard/generation-status/?job_id={job.id}")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["job"]["id"], job.id)
        self.assertEqual(payload["job"]["current_stage"], "SCORE_V8")
        self.assertEqual(payload["job"]["progress_pct"], 34)
        self.assertEqual(payload["stages"][0]["name"], "INGEST")
        self.assertEqual(payload["stages"][0]["status"], PipelineStageRun.STATUS_SUCCESS)
