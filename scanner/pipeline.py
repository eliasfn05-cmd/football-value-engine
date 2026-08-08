from __future__ import annotations

import io
import time as time_module
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable

from django.core.management import call_command
from django.db import transaction
from django.utils import timezone

from backtesting.models import PredictionOutcome
from engine.models import Fixture, Prediction
from engine.score_v8 import V8_MODEL_VERSION
from scanner.models import PipelineRun, PipelineStageRun


@dataclass(frozen=True)
class StageResult:
    records_processed: int = 0
    message: str = ""
    details: dict | None = None


class DailyPipeline:
    """Production orchestrator for the football-value workflow.

    Modes:
    - full: detailed INGEST -> SCORE_V8 -> SETTLE -> LEARNING
    - morning: fast fixture INGEST -> SCORE_V8
    - settlement: SETTLE -> LEARNING

    The morning pass intentionally avoids per-fixture lineup/statistics calls.
    Detailed context is collected by later refreshes when it is actually
    available and useful.
    """

    MODES = {"full", "morning", "settlement"}

    def __init__(self, *, max_attempts: int = 3, retry_delay_seconds: float = 1.0):
        self.max_attempts = max(1, int(max_attempts))
        self.retry_delay_seconds = max(0.0, float(retry_delay_seconds))

    @staticmethod
    def _date_bounds(target_date: date):
        start = timezone.make_aware(datetime.combine(target_date, time.min))
        return start, start + timedelta(days=1)

    @staticmethod
    def _capture_command(name: str, **options) -> str:
        out = io.StringIO()
        call_command(name, stdout=out, **options)
        return out.getvalue()

    def _ingest(self, target_date: date, *, fixtures_only: bool = False) -> StageResult:
        self._capture_command(
            "ingest_daily",
            target_date=target_date.isoformat(),
            fixtures_only=fixtures_only,
        )
        start, end = self._date_bounds(target_date)
        count = Fixture.objects.filter(kickoff__gte=start, kickoff__lt=end).count()
        mode = "fast" if fixtures_only else "detailed"
        return StageResult(count, f"{count} fixtures stored ({mode} ingestion)", {"fixtures": count, "ingestion_mode": mode})

    def _score(self, target_date: date) -> StageResult:
        self._capture_command("score_v8", target_date=target_date.isoformat())
        start, end = self._date_bounds(target_date)
        qs = Prediction.objects.filter(
            model_version=V8_MODEL_VERSION,
            fixture__kickoff__gte=start,
            fixture__kickoff__lt=end,
        )
        count = qs.count()
        premium = qs.filter(tier="TIER_A").count()
        return StageResult(count, f"{count} predictions; {premium} premium", {"predictions": count, "premium": premium})

    def _settle(self, target_date: date) -> StageResult:
        before = PredictionOutcome.objects.filter(prediction__model_version=V8_MODEL_VERSION).count()
        self._capture_command("settle_predictions", model_version=V8_MODEL_VERSION)
        after = PredictionOutcome.objects.filter(prediction__model_version=V8_MODEL_VERSION).count()
        processed = max(0, after - before)
        total_settled = PredictionOutcome.objects.filter(
            prediction__model_version=V8_MODEL_VERSION,
        ).exclude(result=PredictionOutcome.RESULT_PENDING).count()
        return StageResult(processed, f"{processed} newly settled; {total_settled} total", {"new": processed, "total": total_settled})

    def _learning(self, target_date: date) -> StageResult:
        self._capture_command("learning_report", model_version=V8_MODEL_VERSION, premium_only=True)
        settled = PredictionOutcome.objects.filter(
            prediction__model_version=V8_MODEL_VERSION,
            prediction__tier="TIER_A",
        ).exclude(result=PredictionOutcome.RESULT_PENDING).count()
        return StageResult(settled, f"learning report refreshed from {settled} premium outcomes", {"premium_outcomes": settled})

    def _run_stage(self, pipeline: PipelineRun, name: str, fn: Callable[[], StageResult], *, required: bool) -> PipelineStageRun:
        stage = PipelineStageRun.objects.create(pipeline=pipeline, name=name)
        started = timezone.now()
        last_error = None
        print(f"[pipeline #{pipeline.id}] START {name}", flush=True)

        for attempt in range(1, self.max_attempts + 1):
            stage.attempt_count = attempt
            stage.save(update_fields=["attempt_count"])
            print(f"[pipeline #{pipeline.id}] {name} attempt {attempt}/{self.max_attempts}", flush=True)
            try:
                result = fn()
                finished = timezone.now()
                stage.status = PipelineStageRun.STATUS_SUCCESS
                stage.finished_at = finished
                stage.duration_seconds = max(0, int((finished - started).total_seconds()))
                stage.records_processed = result.records_processed
                stage.message = result.message[:255]
                stage.details = result.details or {}
                stage.save()
                print(
                    f"[pipeline #{pipeline.id}] DONE {name}: {result.message} ({stage.duration_seconds}s)",
                    flush=True,
                )
                return stage
            except Exception as exc:
                last_error = exc
                print(f"[pipeline #{pipeline.id}] ERROR {name} attempt {attempt}: {exc}", flush=True)
                if attempt < self.max_attempts and self.retry_delay_seconds:
                    time_module.sleep(self.retry_delay_seconds)

        finished = timezone.now()
        stage.status = PipelineStageRun.STATUS_FAILED if required else PipelineStageRun.STATUS_WARNING
        stage.finished_at = finished
        stage.duration_seconds = max(0, int((finished - started).total_seconds()))
        stage.message = str(last_error)[:255] if last_error else "unknown stage error"
        stage.details = {"error_type": type(last_error).__name__ if last_error else "UnknownError"}
        stage.save()
        print(f"[pipeline #{pipeline.id}] FAILED {name}: {stage.message}", flush=True)
        return stage

    @transaction.atomic
    def _create_run(self, target_date: date, mode: str) -> PipelineRun:
        return PipelineRun.objects.create(
            target_date=target_date,
            metadata={"model_version": V8_MODEL_VERSION, "mode": mode},
        )

    def _stages_for(self, target_date: date, mode: str):
        if mode == "morning":
            return [
                ("INGEST", lambda: self._ingest(target_date, fixtures_only=True), True),
                ("SCORE_V8", lambda: self._score(target_date), True),
            ]
        if mode == "settlement":
            return [
                ("SETTLE", lambda: self._settle(target_date), False),
                ("LEARNING", lambda: self._learning(target_date), False),
            ]
        return [
            ("INGEST", lambda: self._ingest(target_date, fixtures_only=False), True),
            ("SCORE_V8", lambda: self._score(target_date), True),
            ("SETTLE", lambda: self._settle(target_date), False),
            ("LEARNING", lambda: self._learning(target_date), False),
        ]

    def run(self, target_date: date, *, mode: str = "full") -> PipelineRun:
        mode = str(mode).lower().strip()
        if mode not in self.MODES:
            raise ValueError(f"Unsupported pipeline mode: {mode}")

        pipeline = self._create_run(target_date, mode)
        started = timezone.now()
        stages = self._stages_for(target_date, mode)
        print(f"[pipeline #{pipeline.id}] mode={mode} date={target_date} stages={len(stages)}", flush=True)

        required_failed = False
        ingest_failed = False
        warning_count = 0
        error_count = 0
        for name, fn, required in stages:
            if name == "SCORE_V8" and ingest_failed:
                PipelineStageRun.objects.create(
                    pipeline=pipeline,
                    name=name,
                    status=PipelineStageRun.STATUS_WARNING,
                    attempt_count=0,
                    finished_at=timezone.now(),
                    message="skipped because ingestion failed",
                    details={"dependency": "INGEST"},
                )
                warning_count += 1
                print(f"[pipeline #{pipeline.id}] SKIP SCORE_V8 because INGEST failed", flush=True)
                continue

            stage = self._run_stage(pipeline, name, fn, required=required)
            if stage.status == PipelineStageRun.STATUS_FAILED:
                required_failed = True
                error_count += 1
                if name == "INGEST":
                    ingest_failed = True
            elif stage.status == PipelineStageRun.STATUS_WARNING:
                warning_count += 1

        start, end = self._date_bounds(target_date)
        fixtures_count = Fixture.objects.filter(kickoff__gte=start, kickoff__lt=end).count()
        predictions = Prediction.objects.filter(
            model_version=V8_MODEL_VERSION,
            fixture__kickoff__gte=start,
            fixture__kickoff__lt=end,
        )
        predictions_count = predictions.count()
        premium_count = predictions.filter(tier="TIER_A").count()
        settled_count = PredictionOutcome.objects.filter(
            prediction__model_version=V8_MODEL_VERSION,
        ).exclude(result=PredictionOutcome.RESULT_PENDING).count()

        finished = timezone.now()
        pipeline.finished_at = finished
        pipeline.duration_seconds = max(0, int((finished - started).total_seconds()))
        pipeline.fixtures_count = fixtures_count
        pipeline.predictions_count = predictions_count
        pipeline.premium_count = premium_count
        pipeline.settled_count = settled_count
        pipeline.warning_count = warning_count
        pipeline.error_count = error_count
        if required_failed:
            pipeline.status = PipelineRun.STATUS_FAILED
        elif warning_count:
            pipeline.status = PipelineRun.STATUS_PARTIAL
        else:
            pipeline.status = PipelineRun.STATUS_SUCCESS
        pipeline.save()
        print(
            f"[pipeline #{pipeline.id}] FINISH status={pipeline.status} fixtures={fixtures_count} "
            f"predictions={predictions_count} premium={premium_count} duration={pipeline.duration_seconds}s",
            flush=True,
        )
        return pipeline
