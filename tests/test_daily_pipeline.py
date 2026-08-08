from datetime import date
from unittest.mock import patch

from django.test import TestCase

from scanner.models import PipelineRun, PipelineStageRun
from scanner.pipeline import DailyPipeline, StageResult


class DailyPipelineTests(TestCase):
    def test_successful_pipeline_persists_all_stages(self):
        pipeline = DailyPipeline(max_attempts=2, retry_delay_seconds=0)
        with (
            patch.object(pipeline, "_ingest", return_value=StageResult(12, "12 fixtures", {"fixtures": 12})),
            patch.object(pipeline, "_score", return_value=StageResult(24, "24 predictions", {"predictions": 24, "premium": 2})),
            patch.object(pipeline, "_settle", return_value=StageResult(3, "3 settled", {"new": 3})),
            patch.object(pipeline, "_learning", return_value=StageResult(8, "learning refreshed", {"premium_outcomes": 8})),
        ):
            run = pipeline.run(date(2026, 8, 8))

        self.assertEqual(run.status, PipelineRun.STATUS_SUCCESS)
        self.assertEqual(run.stages.count(), 4)
        self.assertEqual(run.error_count, 0)
        self.assertEqual(run.warning_count, 0)
        self.assertTrue(all(stage.status == PipelineStageRun.STATUS_SUCCESS for stage in run.stages.all()))

    def test_stage_retries_before_success(self):
        pipeline = DailyPipeline(max_attempts=3, retry_delay_seconds=0)
        attempts = {"count": 0}

        def flaky():
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise RuntimeError("temporary provider failure")
            return StageResult(5, "recovered")

        run = PipelineRun.objects.create(target_date=date(2026, 8, 8))
        stage = pipeline._run_stage(run, "INGEST", flaky, required=True)
        self.assertEqual(stage.status, PipelineStageRun.STATUS_SUCCESS)
        self.assertEqual(stage.attempt_count, 3)
        self.assertEqual(stage.records_processed, 5)

    def test_failed_ingestion_skips_score_but_keeps_historical_stages(self):
        pipeline = DailyPipeline(max_attempts=2, retry_delay_seconds=0)
        with (
            patch.object(pipeline, "_ingest", side_effect=RuntimeError("API unavailable")),
            patch.object(pipeline, "_score") as score,
            patch.object(pipeline, "_settle", return_value=StageResult(0, "settled")),
            patch.object(pipeline, "_learning", return_value=StageResult(0, "learning")),
        ):
            run = pipeline.run(date(2026, 8, 8))

        self.assertEqual(run.status, PipelineRun.STATUS_FAILED)
        score.assert_not_called()
        ingest = run.stages.get(name="INGEST")
        scoring = run.stages.get(name="SCORE_V8")
        self.assertEqual(ingest.status, PipelineStageRun.STATUS_FAILED)
        self.assertEqual(ingest.attempt_count, 2)
        self.assertEqual(scoring.status, PipelineStageRun.STATUS_WARNING)
        self.assertIn("ingestion failed", scoring.message)
