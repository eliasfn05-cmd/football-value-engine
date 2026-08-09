from datetime import date
from unittest.mock import patch

from django.test import TestCase

from scanner.models import PipelineRun, PipelineStageRun, PremiumGenerationJob
from scanner.pipeline import DailyPipeline, StageResult


class DailyPipelineTests(TestCase):
    def test_successful_pipeline_persists_all_stages_and_uses_fast_ingest(self):
        pipeline = DailyPipeline(max_attempts=2, retry_delay_seconds=0)
        with (
            patch.object(pipeline, "_ingest", return_value=StageResult(12, "12 fixtures", {"fixtures": 12})) as ingest,
            patch.object(pipeline, "_score", return_value=StageResult(24, "24 predictions", {"predictions": 24})) as score,
            patch.object(pipeline, "_enrich", return_value=StageResult(8, "8 enriched", {"candidates": 8})) as enrich,
            patch.object(pipeline, "_rescore_enriched", return_value=StageResult(8, "8 rescored", {"raw_tier_a": 2})) as rescore,
            patch.object(pipeline, "_deep_analysis", return_value=StageResult(12, "12 deep", {"preferred_markets": 6})) as deep,
            patch.object(pipeline, "_select_premium", return_value=StageResult(3, "3 selected", {"selected": 3})) as select,
            patch.object(pipeline, "_settle", return_value=StageResult(3, "3 settled", {"new": 3})),
            patch.object(pipeline, "_learning", return_value=StageResult(8, "learning refreshed", {"premium_outcomes": 8})),
        ):
            run = pipeline.run(date(2026, 8, 8))

        self.assertEqual(run.status, PipelineRun.STATUS_SUCCESS)
        self.assertEqual(
            list(run.stages.values_list("name", flat=True)),
            ["INGEST", "SCORE_V8", "ENRICH_CANDIDATES", "RESCORE_V8", "DEEP_ANALYSIS", "SELECT_PREMIUM", "SETTLE", "LEARNING"],
        )
        self.assertTrue(all(stage.status == PipelineStageRun.STATUS_SUCCESS for stage in run.stages.all()))
        ingest.assert_called_once_with(date(2026, 8, 8), fixtures_only=True)
        score.assert_called_once_with(date(2026, 8, 8))
        enrich.assert_called_once_with(date(2026, 8, 8))
        rescore.assert_called_once_with(date(2026, 8, 8))
        deep.assert_called_once_with(date(2026, 8, 8))
        select.assert_called_once_with(date(2026, 8, 8))

    def test_detailed_mode_is_explicit_and_uses_deep_analysis(self):
        pipeline = DailyPipeline(max_attempts=1, retry_delay_seconds=0)
        with (
            patch.object(pipeline, "_ingest", return_value=StageResult(10, "detailed")) as ingest,
            patch.object(pipeline, "_score", return_value=StageResult(20, "scored")),
            patch.object(pipeline, "_deep_analysis", return_value=StageResult(8, "deep")) as deep,
            patch.object(pipeline, "_select_premium", return_value=StageResult(1, "selected")),
            patch.object(pipeline, "_settle", return_value=StageResult(0, "settled")),
            patch.object(pipeline, "_learning", return_value=StageResult(0, "learned")),
        ):
            run = pipeline.run(date(2026, 8, 8), mode="detailed")
        ingest.assert_called_once_with(date(2026, 8, 8), fixtures_only=False)
        deep.assert_called_once()

    def test_morning_mode_runs_only_ingest_and_score(self):
        pipeline = DailyPipeline(max_attempts=1, retry_delay_seconds=0)
        with (
            patch.object(pipeline, "_ingest", return_value=StageResult(10, "ingested")) as ingest,
            patch.object(pipeline, "_score", return_value=StageResult(20, "scored")) as score,
            patch.object(pipeline, "_deep_analysis") as deep,
            patch.object(pipeline, "_select_premium") as select,
            patch.object(pipeline, "_settle") as settle,
            patch.object(pipeline, "_learning") as learning,
        ):
            run = pipeline.run(date(2026, 8, 8), mode="morning")
        self.assertEqual(list(run.stages.values_list("name", flat=True)), ["INGEST", "SCORE_V8"])
        ingest.assert_called_once_with(date(2026, 8, 8), fixtures_only=True)
        score.assert_called_once()
        deep.assert_not_called()
        select.assert_not_called()
        settle.assert_not_called()
        learning.assert_not_called()

    def test_refresh_mode_enriches_rescores_deep_analyzes_and_reselects(self):
        pipeline = DailyPipeline(max_attempts=1, retry_delay_seconds=0)
        with (
            patch.object(pipeline, "_ingest") as ingest,
            patch.object(pipeline, "_enrich", return_value=StageResult(16, "enriched")) as enrich,
            patch.object(pipeline, "_rescore_enriched", return_value=StageResult(16, "rescored")) as rescore,
            patch.object(pipeline, "_deep_analysis", return_value=StageResult(12, "deep")) as deep,
            patch.object(pipeline, "_select_premium", return_value=StageResult(3, "selected")) as select,
            patch.object(pipeline, "_score") as score,
            patch.object(pipeline, "_settle") as settle,
            patch.object(pipeline, "_learning") as learning,
        ):
            run = pipeline.run(date(2026, 8, 8), mode="refresh")
        self.assertEqual(
            list(run.stages.values_list("name", flat=True)),
            ["ENRICH_CANDIDATES", "RESCORE_V8", "DEEP_ANALYSIS", "SELECT_PREMIUM"],
        )
        ingest.assert_not_called()
        enrich.assert_called_once()
        rescore.assert_called_once()
        deep.assert_called_once()
        select.assert_called_once()
        score.assert_not_called()
        settle.assert_not_called()
        learning.assert_not_called()

    def test_settlement_mode_runs_only_historical_stages(self):
        pipeline = DailyPipeline(max_attempts=1, retry_delay_seconds=0)
        with (
            patch.object(pipeline, "_settle", return_value=StageResult(2, "settled")) as settle,
            patch.object(pipeline, "_learning", return_value=StageResult(5, "learned")) as learning,
        ):
            run = pipeline.run(date(2026, 8, 8), mode="settlement")
        self.assertEqual(list(run.stages.values_list("name", flat=True)), ["SETTLE", "LEARNING"])
        settle.assert_called_once()
        learning.assert_called_once()

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

    def test_failed_ingestion_skips_deep_analysis_too(self):
        pipeline = DailyPipeline(max_attempts=1, retry_delay_seconds=0)
        with (
            patch.object(pipeline, "_ingest", side_effect=RuntimeError("API unavailable")),
            patch.object(pipeline, "_score") as score,
            patch.object(pipeline, "_enrich") as enrich,
            patch.object(pipeline, "_rescore_enriched") as rescore,
            patch.object(pipeline, "_deep_analysis") as deep,
            patch.object(pipeline, "_select_premium") as select,
            patch.object(pipeline, "_settle", return_value=StageResult(0, "settled")),
            patch.object(pipeline, "_learning", return_value=StageResult(0, "learning")),
        ):
            run = pipeline.run(date(2026, 8, 8), mode="full")
        self.assertEqual(run.status, PipelineRun.STATUS_FAILED)
        score.assert_not_called(); enrich.assert_not_called(); rescore.assert_not_called(); deep.assert_not_called(); select.assert_not_called()
        self.assertEqual(run.stages.get(name="DEEP_ANALYSIS").status, PipelineStageRun.STATUS_WARNING)

    def test_generation_job_is_claimed_and_completed_by_pipeline(self):
        job = PremiumGenerationJob.objects.create(target_date=date(2026, 8, 8), status=PremiumGenerationJob.STATUS_DISPATCHED, mode="full")
        pipeline = DailyPipeline(max_attempts=1, retry_delay_seconds=0)
        with (
            patch.object(pipeline, "_ingest", return_value=StageResult(1, "ingested")),
            patch.object(pipeline, "_score", return_value=StageResult(2, "scored")),
            patch.object(pipeline, "_enrich", return_value=StageResult(1, "enriched")),
            patch.object(pipeline, "_rescore_enriched", return_value=StageResult(1, "rescored")),
            patch.object(pipeline, "_deep_analysis", return_value=StageResult(2, "deep")),
            patch.object(pipeline, "_select_premium", return_value=StageResult(0, "NO BET")),
            patch.object(pipeline, "_settle", return_value=StageResult(0, "settled")),
            patch.object(pipeline, "_learning", return_value=StageResult(0, "learned")),
        ):
            run = pipeline.run(date(2026, 8, 8), generation_job_id=job.id)
        job.refresh_from_db()
        self.assertEqual(job.pipeline_id, run.id)
        self.assertEqual(job.status, PremiumGenerationJob.STATUS_SUCCESS)
        self.assertEqual(job.progress_pct, 100)

    def test_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            DailyPipeline(retry_delay_seconds=0).run(date(2026, 8, 8), mode="nightly")
