from __future__ import annotations

import os
import sys
import time as time_module
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable

from django.core.management import call_command
from django.db import transaction
from django.utils import timezone

from backtesting.models import PredictionOutcome
from engine.candidate_pool import CandidatePoolRule, high_recall_candidate_pool
from engine.models import DailyPremiumSelection, Fixture, Prediction
from engine.score_v8 import V8_MODEL_VERSION
from scanner.models import PipelineRun, PipelineStageRun, PremiumGenerationJob


@dataclass(frozen=True)
class StageResult:
    records_processed: int = 0
    message: str = ""
    details: dict | None = None


class DailyPipeline:
    """Production orchestrator for the football-value workflow."""

    MODES = {"full", "morning", "refresh", "settlement", "detailed"}
    INTERACTIVE_POOL_LIMIT = 12

    def __init__(self, *, max_attempts: int = 3, retry_delay_seconds: float = 1.0):
        self.max_attempts = max(1, int(max_attempts))
        self.retry_delay_seconds = max(0.0, float(retry_delay_seconds))
        self._candidate_pool_cache: dict[date, list[int]] = {}

    @staticmethod
    def _interactive_fast_enabled() -> bool:
        return os.getenv("PREMIUM_INTERACTIVE_FAST", "").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _date_bounds(target_date: date):
        start = timezone.make_aware(datetime.combine(target_date, time.min))
        return start, start + timedelta(days=1)

    @staticmethod
    def _run_command(name: str, **options) -> None:
        call_command(name, stdout=sys.stdout, stderr=sys.stderr, **options)

    @staticmethod
    def _sync_generation_job(job: PremiumGenerationJob | None, **updates) -> None:
        if job is None:
            return
        for field, value in updates.items():
            setattr(job, field, value)
        job.save(update_fields=list(updates.keys()))

    def _ingest(self, target_date: date, *, fixtures_only: bool = False) -> StageResult:
        self._run_command("ingest_daily", target_date=target_date.isoformat(), fixtures_only=fixtures_only)
        start, end = self._date_bounds(target_date)
        count = Fixture.objects.filter(kickoff__gte=start, kickoff__lt=end).count()
        mode = "fast" if fixtures_only else "detailed"
        return StageResult(count, f"{count} fixtures stored ({mode} ingestion)", {"fixtures": count, "ingestion_mode": mode})

    def _score(self, target_date: date) -> StageResult:
        self._run_command("score_v8", target_date=target_date.isoformat(), summary_only=True)
        start, end = self._date_bounds(target_date)
        qs = Prediction.objects.filter(
            model_version=V8_MODEL_VERSION,
            fixture__kickoff__gte=start,
            fixture__kickoff__lt=end,
        )
        count = qs.count()
        premium = qs.filter(tier="TIER_A").count()
        return StageResult(count, f"{count} predictions; {premium} raw tier-A", {"predictions": count, "raw_tier_a": premium})

    def _future_candidate_fixture_ids(self, target_date: date, *, limit: int = 30) -> list[int]:
        if self._interactive_fast_enabled():
            cached = self._candidate_pool_cache.get(target_date)
            if cached is not None:
                return cached[:limit]
            pool = high_recall_candidate_pool(
                target_date,
                rule=CandidatePoolRule(limit=min(limit, self.INTERACTIVE_POOL_LIMIT)),
            )
            fixture_ids = [entry.fixture_id for entry in pool]
            self._candidate_pool_cache[target_date] = fixture_ids
            return fixture_ids

        start, end = self._date_bounds(target_date)
        future_start = max(start, timezone.now())
        qs = (
            Prediction.objects.filter(
                model_version=V8_MODEL_VERSION,
                fixture__kickoff__gte=future_start,
                fixture__kickoff__lt=end,
                score__gte=50,
            )
            .select_related("fixture")
            .order_by("-tier", "-score", "fixture__kickoff")
        )
        fixture_ids: list[int] = []
        seen: set[int] = set()
        for prediction in qs.iterator(chunk_size=500):
            if prediction.fixture_id in seen:
                continue
            seen.add(prediction.fixture_id)
            fixture_ids.append(prediction.fixture_id)
            if len(fixture_ids) >= limit:
                break
        return fixture_ids

    def _enrich(self, target_date: date) -> StageResult:
        limit = self.INTERACTIVE_POOL_LIMIT if self._interactive_fast_enabled() else 30
        fixture_ids = self._future_candidate_fixture_ids(target_date, limit=limit)
        self._run_command(
            "enrich_candidates",
            target_date=target_date.isoformat(),
            limit=limit,
            min_score=50.0,
        )
        processed = len(fixture_ids)
        mode = "high-recall" if self._interactive_fast_enabled() else "standard"
        return StageResult(
            processed,
            f"{processed} future shortlisted fixtures enriched ({mode})",
            {"candidates": processed, "pool_mode": mode, "fixture_ids": fixture_ids},
        )

    def _rescore_enriched(self, target_date: date) -> StageResult:
        limit = self.INTERACTIVE_POOL_LIMIT if self._interactive_fast_enabled() else 30
        fixture_ids = self._future_candidate_fixture_ids(target_date, limit=limit)
        fixtures = list(
            Fixture.objects.filter(id__in=fixture_ids)
            .select_related("home_team", "away_team")
        )
        fixture_map = {fixture.id: fixture for fixture in fixtures}
        rescored = 0
        for fixture_id in fixture_ids:
            fixture = fixture_map.get(fixture_id)
            if fixture is None or fixture.kickoff <= timezone.now():
                continue
            self._run_command("score_v8", fixture_id=fixture.external_id)
            rescored += 1

        start, end = self._date_bounds(target_date)
        future_start = max(start, timezone.now())
        future_qs = Prediction.objects.filter(
            model_version=V8_MODEL_VERSION,
            fixture__kickoff__gte=future_start,
            fixture__kickoff__lt=end,
        )
        premium = future_qs.filter(tier="TIER_A").count()
        return StageResult(
            rescored,
            f"{rescored} enriched future fixtures rescored; {premium} raw tier-A",
            {"rescored": rescored, "raw_tier_a": premium, "fixture_ids": fixture_ids},
        )

    def _select_premium(self, target_date: date) -> StageResult:
        self._run_command("select_premium", target_date=target_date.isoformat(), max_picks=3)
        rows = DailyPremiumSelection.objects.filter(
            target_date=target_date,
            model_version=V8_MODEL_VERSION,
        ).order_by("rank")
        count = rows.count()
        tiers = list(rows.values_list("premium_tier", flat=True))
        message = f"{count} operational Premium picks selected" if count else "NO BET: 0 operational Premium picks"
        return StageResult(count, message, {"selected": count, "tiers": tiers})

    def _settle(self, target_date: date) -> StageResult:
        before = PredictionOutcome.objects.filter(prediction__model_version=V8_MODEL_VERSION).count()
        self._run_command("settle_predictions", model_version=V8_MODEL_VERSION)
        after = PredictionOutcome.objects.filter(prediction__model_version=V8_MODEL_VERSION).count()
        processed = max(0, after - before)
        total_settled = PredictionOutcome.objects.filter(prediction__model_version=V8_MODEL_VERSION).exclude(
            result=PredictionOutcome.RESULT_PENDING
        ).count()
        return StageResult(processed, f"{processed} newly settled; {total_settled} total", {"new": processed, "total": total_settled})

    def _learning(self, target_date: date) -> StageResult:
        self._run_command("learning_report", model_version=V8_MODEL_VERSION, premium_only=True)
        settled = PredictionOutcome.objects.filter(
            prediction__model_version=V8_MODEL_VERSION,
            prediction__tier="TIER_A",
        ).exclude(result=PredictionOutcome.RESULT_PENDING).count()
        return StageResult(settled, f"learning report refreshed from {settled} raw tier-A outcomes", {"premium_outcomes": settled})

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
                print(f"[pipeline #{pipeline.id}] DONE {name}: {result.message} ({stage.duration_seconds}s)", flush=True)
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
    def _create_run(self, target_date: date, mode: str, generation_job_id: int | None = None) -> tuple[PipelineRun, PremiumGenerationJob | None]:
        pipeline = PipelineRun.objects.create(target_date=target_date, metadata={"model_version": V8_MODEL_VERSION, "mode": mode})
        job = None
        if generation_job_id is not None:
            job = PremiumGenerationJob.objects.select_for_update().filter(pk=generation_job_id).first()
            if job is None:
                raise ValueError(f"PremiumGenerationJob #{generation_job_id} not found")
            if job.target_date != target_date:
                raise ValueError(f"PremiumGenerationJob #{generation_job_id} target date mismatch")
            job.pipeline = pipeline
            job.status = PremiumGenerationJob.STATUS_RUNNING
            job.current_stage = "STARTING"
            job.progress_pct = 1
            job.message = "Worker iniciado; preparando pipeline."
            job.started_at = timezone.now()
            job.save(update_fields=["pipeline", "status", "current_stage", "progress_pct", "message", "started_at"])
        return pipeline, job

    def _stages_for(self, target_date: date, mode: str):
        if mode == "morning":
            return [
                ("INGEST", lambda: self._ingest(target_date, fixtures_only=True), True),
                ("SCORE_V8", lambda: self._score(target_date), True),
            ]
        if mode == "refresh":
            return [
                ("ENRICH_CANDIDATES", lambda: self._enrich(target_date), False),
                ("RESCORE_V8", lambda: self._rescore_enriched(target_date), True),
                ("SELECT_PREMIUM", lambda: self._select_premium(target_date), True),
            ]
        if mode == "settlement":
            return [
                ("SETTLE", lambda: self._settle(target_date), False),
                ("LEARNING", lambda: self._learning(target_date), False),
            ]
        if mode == "detailed":
            return [
                ("INGEST", lambda: self._ingest(target_date, fixtures_only=False), True),
                ("SCORE_V8", lambda: self._score(target_date), True),
                ("SELECT_PREMIUM", lambda: self._select_premium(target_date), True),
                ("SETTLE", lambda: self._settle(target_date), False),
                ("LEARNING", lambda: self._learning(target_date), False),
            ]
        return [
            ("INGEST", lambda: self._ingest(target_date, fixtures_only=True), True),
            ("SCORE_V8", lambda: self._score(target_date), True),
            ("ENRICH_CANDIDATES", lambda: self._enrich(target_date), False),
            ("RESCORE_V8", lambda: self._rescore_enriched(target_date), True),
            ("SELECT_PREMIUM", lambda: self._select_premium(target_date), True),
            ("SETTLE", lambda: self._settle(target_date), False),
            ("LEARNING", lambda: self._learning(target_date), False),
        ]

    def run(self, target_date: date, *, mode: str = "full", generation_job_id: int | None = None) -> PipelineRun:
        mode = str(mode).lower().strip()
        if mode not in self.MODES:
            raise ValueError(f"Unsupported pipeline mode: {mode}")
        pipeline, generation_job = self._create_run(target_date, mode, generation_job_id)
        started = timezone.now()
        stages = self._stages_for(target_date, mode)
        print(f"[pipeline #{pipeline.id}] mode={mode} date={target_date} stages={len(stages)}", flush=True)
        required_failed = False
        ingest_failed = False
        warning_count = 0
        error_count = 0
        dependent_on_ingest = {"SCORE_V8", "ENRICH_CANDIDATES", "RESCORE_V8", "SELECT_PREMIUM"}

        for index, (name, fn, required) in enumerate(stages, start=1):
            start_progress = max(2, int(((index - 1) / max(1, len(stages))) * 95))
            self._sync_generation_job(
                generation_job,
                current_stage=name,
                progress_pct=start_progress,
                message=f"Ejecutando {name}…",
            )
            if name in dependent_on_ingest and ingest_failed:
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
                print(f"[pipeline #{pipeline.id}] SKIP {name} because INGEST failed", flush=True)
            else:
                stage = self._run_stage(pipeline, name, fn, required=required)
                if stage.status == PipelineStageRun.STATUS_FAILED:
                    required_failed = True
                    error_count += 1
                    if name == "INGEST":
                        ingest_failed = True
                elif stage.status == PipelineStageRun.STATUS_WARNING:
                    warning_count += 1
            end_progress = min(97, int((index / max(1, len(stages))) * 97))
            self._sync_generation_job(
                generation_job,
                progress_pct=end_progress,
                message=f"{name} completado.",
            )

        start, end = self._date_bounds(target_date)
        fixtures_count = Fixture.objects.filter(kickoff__gte=start, kickoff__lt=end).count()
        predictions = Prediction.objects.filter(
            model_version=V8_MODEL_VERSION,
            fixture__kickoff__gte=start,
            fixture__kickoff__lt=end,
        )
        predictions_count = predictions.count()
        premium_count = DailyPremiumSelection.objects.filter(
            target_date=target_date,
            model_version=V8_MODEL_VERSION,
        ).count()
        settled_count = PredictionOutcome.objects.filter(prediction__model_version=V8_MODEL_VERSION).exclude(
            result=PredictionOutcome.RESULT_PENDING
        ).count()
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

        if generation_job is not None:
            status_map = {
                PipelineRun.STATUS_SUCCESS: PremiumGenerationJob.STATUS_SUCCESS,
                PipelineRun.STATUS_PARTIAL: PremiumGenerationJob.STATUS_PARTIAL,
                PipelineRun.STATUS_FAILED: PremiumGenerationJob.STATUS_FAILED,
            }
            generation_job.status = status_map[pipeline.status]
            generation_job.current_stage = "COMPLETE" if pipeline.status != PipelineRun.STATUS_FAILED else "FAILED"
            generation_job.progress_pct = 100
            generation_job.message = (
                f"Completado: {premium_count} Pick(s) Premium."
                if pipeline.status != PipelineRun.STATUS_FAILED
                else "El pipeline terminó con errores."
            )
            generation_job.finished_at = finished
            generation_job.metadata = {
                **(generation_job.metadata or {}),
                "fixtures_count": fixtures_count,
                "predictions_count": predictions_count,
                "premium_count": premium_count,
                "duration_seconds": pipeline.duration_seconds,
            }
            generation_job.save(update_fields=["status", "current_stage", "progress_pct", "message", "finished_at", "metadata"])

        print(
            f"[pipeline #{pipeline.id}] FINISH status={pipeline.status} fixtures={fixtures_count} "
            f"predictions={predictions_count} premium={premium_count} duration={pipeline.duration_seconds}s",
            flush=True,
        )
        return pipeline
