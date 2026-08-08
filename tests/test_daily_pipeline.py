from datetime import date
from unittest.mock import patch

from django.test import TestCase

from scanner.models import PipelineRun, PipelineStageRun
from scanner.pipeline import DailyPipeline, StageResult


class DailyPipelineTests(TestCase):
    def test_successful_pipeline_persists_all_stages_and_uses_fast_ingest(self):
        pipeline = DailyPipeline(max_attempts=2, retry_delay_seconds=0)
        with (
            patch.object(pipeline, "_ingest", return_value=StageResult(12, "12 fixtures", {"fixtures": 12})) as ingest,
            patch.object(pipeline, "_score", return_value=StageResult(24, "24 predictions", {"predictions": 24})) as score,
            patch.object(pipeline, "_enrich", return_value=StageResult(8, "8 enriched", {"candidates": 8})) as enrich,
            patch.object(pipeline, "_rescore_enriched", return_value=StageResult(8, "8 rescored", {"raw_tier_a": 2})) as rescore,
            patch.object(pipeline, "_select_premium", return_value=StageResult(3, "3 selected", {"selected": 3})) as select,
            patch.object(pipeline, "_settle", return_value=StageResult(3, "3 settled", {"new": 3})),
            patch.object(pipeline, "_learning", return_value=StageResult(8, "learning refreshed", {"premium_outcomes": 8})),
        ):
            run = pipeline.run(date(2026, 8, 8))

        self.assertEqual(run.status, PipelineRun.STATUS_SUCCESS)
        self.assertEqual(
            list(run.stages.values_list("name", flat=True)),
            ["INGEST", "SCORE_V8", "ENRICH_CANDIDATES", "RESCORE_V8", "SELECT_PREMIUM", "SETTLE", "LEARNING"],
        )
        self.assertEqual(run.metadata["mode"], "full")
        self.assertTrue(all(stage.status == PipelineStageRun.STATUS_SUCCESS for stage in run.stages.all()))
        ingest.assert_called_once_with(date(2026, 8, 8), fixtures_only=True)
        score.assert_called_once_with(date(2026, 8, 8))
        enrich.assert_called_once_with(date(2026, 8, 8))
        rescore.assert_called_once_with(date(2026, 8, 8))
        select.assert_called_once_with(date(2026, 8, 8))

    def test_detailed_mode_is_explicit_and_uses_detailed_ingestion(self):
        pipeline = DailyPipeline(max_attempts=1, retry_delay_seconds=0)
        with (
            patch.object(pipeline, "_ingest", return_value=StageResult(10, "detailed")) as ingest,
            patch.object(pipeline, "_score", return_value=StageResult(20, "scored")),
            patch.object(pipeline, "_select_premium", return_value=StageResult(1, "selected")),
            patch.object(pipeline, "_settle", return_value=StageResult(0, "settled")),
            patch.object(pipeline, "_learning", return_value=StageResult(0, "learned")),
        ):
            run = pipeline.run(date(2026, 8, 8), mode="detailed")

        self.assertEqual(run.metadata["mode"], "detailed")
        ingest.assert_called_once_with(date(2026, 8, 8), fixtures_only=False)

    def test_morning_mode_runs_only_ingest_and_score(self):
        pipeline = DailyPipeline(max_attempts=1, retry_delay_seconds=0)
        with (
            patch.object(pipeline, "_ingest", return_value=StageResult(10, "ingested")) as ingest,
            patch.object(pipeline, "_score", return_value=StageResult(20, "scored")) as score,
            patch.object(pipeline, "_select_premium") as select,
            patch.object(pipeline, "_settle") as settle,
            patch.object(pipeline, "_learning") as learning,
        ):
            run = pipeline.run(date(2026, 8, 8), mode="morning")

        self.assertEqual(run.metadata["mode"], "morning")
        self.assertEqual(list(run.stages.values_list("name", flat=True)), ["INGEST", "SCORE_V8"])
        ingest.assert_called_once_with(date(2026, 8, 8), fixtures_only=True)
        score.assert_called_once()
        select.assert_not_called()
        settle.assert_not_called()
        learning.assert_not_called()

    def test_refresh_mode_enriches_rescores_and_reselects_without_full_ingest(self):
        pipeline = DailyPipeline(max_attempts=1, retry_delay_seconds=0)
        with (
            patch.object(pipeline, "_ingest") as ingest,
            patch.object(pipeline, "_enrich", return_value=StageResult(20, "enriched")) as enrich,
            patch.object(pipeline, "_rescore_enriched", return_value=StageResult(20, "rescored")) as rescore,
            patch.object(pipeline, "_select_premium", return_value=StageResult(3, "selected")) as select,
            patch.object(pipeline, "_score") as score,
            patch.object(pipeline, "_settle") as settle,
            patch.object(pipeline, "_learning") as learning,
        ):
            run = pipeline.run(date(2026, 8, 8), mode="refresh")

        self.assertEqual(run.metadata["mode"], "refresh")
        self.assertEqual(list(run.stages.values_list("name", flat=True)), ["ENRICH_CANDIDATES", "RESCORE_V8", "SELECT_PREMIUM"])
        ingest.assert_not_called()
        enrich.assert_called_once_with(date(2026, 8, 8))
        rescore.assert_called_once_with(date(2026, 8, 8))
        select.assert_called_once_with(date(2026, 8, 8))
        score.assert_not_called()
        settle.assert_not_called()
        learning.assert_not_called()

    def test_settlement_mode_runs_only_historical_stages(self):
        pipeline = DailyPipeline(max_attempts=1, retry_delay_seconds=0)
        with (
            patch.object(pipeline, "_ingest") as ingest,
            patch.object(pipeline, "_score") as score,
            patch.object(pipeline, "_settle", return_value=StageResult(2, "settled")) as settle,
            patch.object(pipeline, "_learning", return_value=StageResult(5, "learned")) as learning,
        ):
            run = pipeline.run(date(2026, 8, 8), mode="settlement")

        self.assertEqual(run.metadata["mode"], "settlement")
        self.assertEqual(list(run.stages.values_list("name", flat=True)), ["SETTLE", "LEARNING"])
        ingest.assert_not_called()
        score.assert_not_called()
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
        self.assertEqual(stage.records_processed, 5)

    def test_failed_ingestion_skips_all_data_dependent_stages_but_keeps_historical_stages(self):
        pipeline = DailyPipeline(max_attempts=2, retry_delay_seconds=0)
        with (
            patch.object(pipeline, "_ingest", side_effect=RuntimeError("API unavailable")),
            patch.object(pipeline, "_score") as score,
            patch.object(pipeline, "_enrich") as enrich,
            patch.object(pipeline, "_rescore_enriched") as rescore,
            patch.object(pipeline, "_select_premium") as select,
            patch.object(pipeline, "_settle", return_value=StageResult(0, "settled")),
            patch.object(pipeline, "_learning", return_value=StageResult(0, "learning")),
        ):
            run = pipeline.run(date(2026, 8, 8), mode="full")

        self.assertEqual(run.status, PipelineRun.STATUS_FAILED)
        score.assert_not_called()
        enrich.assert_not_called()
        rescore.assert_not_called()
        select.assert_not_called()
        ingest = run.stages.get(name="INGEST")
        scoring = run.stages.get(name="SCORE_V8")
        enrichment = run.stages.get(name="ENRICH_CANDIDATES")
        rescore_stage = run.stages.get(name="RESCORE_V8")
        selection = run.stages.get(name="SELECT_PREMIUM")
        self.assertEqual(ingest.status, PipelineStageRun.STATUS_FAILED)
        self.assertEqual(ingest.attempt_count, 2)
        self.assertEqual(scoring.status, PipelineStageRun.STATUS_WARNING)
        self.assertEqual(enrichment.status, PipelineStageRun.STATUS_WARNING)
        self.assertEqual(rescore_stage.status, PipelineStageRun.STATUS_WARNING)
        self.assertEqual(selection.status, PipelineStageRun.STATUS_WARNING)
        self.assertIn("ingestion failed", scoring.message)

    def test_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            DailyPipeline(retry_delay_seconds=0).run(date(2026, 8, 8), mode="nightly")
