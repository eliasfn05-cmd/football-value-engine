from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from time import perf_counter

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, transaction
from django.utils import timezone

from engine.batch_features import BatchFeatureEngineeringService
from engine.candidate_pool import CandidatePoolRule, high_recall_candidate_pool
from engine.models import Fixture, Prediction
from engine.score_v8 import ScoreEngineV8, V8_MODEL_VERSION
from scanner.models import PipelineStageRun, PremiumGenerationJob

PERSIST_BATCH_SIZE = 500
INTERACTIVE_RESCORE_LIMIT = 32
DEFAULT_INTERACTIVE_WORKERS = 8
MAX_RESCORE_WORKERS = 12
_THREAD_LOCAL = threading.local()


class Command(BaseCommand):
    help = "Batch-rescore the Premium candidate pool using one shared feature preload."

    def add_arguments(self, parser):
        parser.add_argument("--date", dest="target_date", required=True, help="YYYY-MM-DD")
        parser.add_argument("--limit", type=int, default=40)
        parser.add_argument("--fixture-ids", default="", help="Comma-separated preselected fixture ids.")

    @staticmethod
    def _interactive_fast_enabled() -> bool:
        return os.getenv("PREMIUM_INTERACTIVE_FAST", "").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _active_job(target_date):
        raw_job_id = os.getenv("PREMIUM_GENERATION_JOB_ID", "").strip()
        if not raw_job_id:
            return None
        try:
            return PremiumGenerationJob.objects.filter(pk=int(raw_job_id), target_date=target_date).first()
        except ValueError:
            return None

    @classmethod
    def _heartbeat(cls, target_date, progress_pct, message):
        job = cls._active_job(target_date)
        if job is None:
            return
        PremiumGenerationJob.objects.filter(pk=job.pk).update(
            current_stage="RESCORE_V8",
            progress_pct=max(23, min(39, int(progress_pct))),
            message=str(message)[:255],
        )

    @staticmethod
    def _worker_count(total: int, interactive: bool) -> int:
        if total <= 1:
            return 1
        default = DEFAULT_INTERACTIVE_WORKERS if interactive else 4
        try:
            configured = int(os.getenv("PREMIUM_RESCORE_WORKERS", str(default)))
        except ValueError:
            configured = default
        return max(1, min(total, configured, MAX_RESCORE_WORKERS))

    @staticmethod
    def _thread_engine() -> ScoreEngineV8:
        engine = getattr(_THREAD_LOCAL, "score_engine", None)
        if engine is None:
            engine = ScoreEngineV8()
            _THREAD_LOCAL.score_engine = engine
        return engine

    @classmethod
    def _evaluate_one(cls, fixture: Fixture, features):
        # Django connections are thread-local. Explicitly retire inherited/stale
        # connections before/after each task so the worker pool is safe on GitHub
        # Actions while allowing the DB-bound confidence lookups to overlap.
        close_old_connections()
        try:
            result = cls._thread_engine().evaluate(fixture, features)
            return fixture, result
        finally:
            close_old_connections()

    def _bulk_persist(self, evaluated_rows):
        if not evaluated_rows:
            return 0, 0
        fixture_ids = {fixture.id for fixture, _ in evaluated_rows}
        existing = {(r.fixture_id, r.market, r.selection): r for r in Prediction.objects.filter(
            fixture_id__in=fixture_ids, model_version=V8_MODEL_VERSION)}
        creates, updates = [], []
        fields = ["probability", "fair_odds", "market_odds", "edge", "expected_value", "score", "tier", "reasons"]
        for fixture, evaluation in evaluated_rows:
            key = (fixture.id, evaluation["market"], evaluation["selection"])
            row = existing.get(key)
            values = {field: evaluation[field] for field in fields}
            if row is None:
                creates.append(Prediction(fixture=fixture, model_version=V8_MODEL_VERSION,
                    market=evaluation["market"], selection=evaluation["selection"], **values))
            else:
                for field, value in values.items():
                    setattr(row, field, value)
                updates.append(row)
        with transaction.atomic():
            if creates:
                Prediction.objects.bulk_create(creates, batch_size=PERSIST_BATCH_SIZE)
            if updates:
                Prediction.objects.bulk_update(updates, fields, batch_size=PERSIST_BATCH_SIZE)
        return len(creates), len(updates)

    @staticmethod
    def _parse_fixture_ids(raw, limit):
        ordered, seen = [], set()
        for token in (raw or "").split(","):
            token = token.strip()
            if not token:
                continue
            try:
                fixture_id = int(token)
            except ValueError as exc:
                raise CommandError(f"Invalid fixture id: {token}") from exc
            if fixture_id > 0 and fixture_id not in seen:
                seen.add(fixture_id)
                ordered.append(fixture_id)
            if len(ordered) >= limit:
                break
        return ordered

    def _ids_from_active_enrichment(self, target_date, limit):
        raw_job_id = os.getenv("PREMIUM_GENERATION_JOB_ID", "").strip()
        if not raw_job_id:
            return []
        try:
            job = PremiumGenerationJob.objects.select_related("pipeline").get(pk=int(raw_job_id), target_date=target_date)
        except (ValueError, PremiumGenerationJob.DoesNotExist):
            return []
        if not job.pipeline_id:
            return []
        stage = (PipelineStageRun.objects.filter(
            pipeline_id=job.pipeline_id,
            name="ENRICH_CANDIDATES",
            status=PipelineStageRun.STATUS_SUCCESS,
        ).order_by("-id").first())
        if not stage:
            return []
        ids = (stage.details or {}).get("fixture_ids") or []
        return self._parse_fixture_ids(",".join(str(value) for value in ids), limit)

    def handle(self, *args, **options):
        total_started = perf_counter()
        try:
            target_date = date.fromisoformat(options["target_date"])
        except ValueError as exc:
            raise CommandError("--date must use YYYY-MM-DD") from exc

        interactive = self._interactive_fast_enabled()
        requested_limit = max(1, min(int(options.get("limit") or 40), 60))
        limit = min(requested_limit, INTERACTIVE_RESCORE_LIMIT) if interactive else requested_limit
        self._heartbeat(target_date, 24, f"RESCORE V8: preparando los mejores {limit} candidatos.")

        ordered_ids = self._parse_fixture_ids(options.get("fixture_ids"), limit)
        source = "explicit"
        if not ordered_ids:
            ordered_ids = self._ids_from_active_enrichment(target_date, limit)
            source = "enrichment-stage"

        pool_seconds = 0.0
        if ordered_ids:
            self.stdout.write(f"[rescore_batch] reusing {len(ordered_ids)} fixture ids from {source}; duplicate pool rebuild skipped")
        else:
            self._heartbeat(target_date, 25, "RESCORE V8: reconstruyendo shortlist optimizado.")
            pool_started = perf_counter()
            pool = high_recall_candidate_pool(target_date, rule=CandidatePoolRule(limit=limit))
            pool_seconds = perf_counter() - pool_started
            ordered_ids = [entry.fixture_id for entry in pool]
            self.stdout.write(f"[rescore_batch] fallback pool rebuild completed in {pool_seconds:.2f}s")

        if not ordered_ids:
            self._heartbeat(target_date, 39, "RESCORE V8: no hay candidatos pendientes.")
            self.stdout.write("[rescore_batch] no candidates to rescore")
            return

        fixtures = list(Fixture.objects.filter(id__in=ordered_ids, kickoff__gt=timezone.now())
            .select_related("home_team", "away_team", "competition_ref"))
        by_id = {fixture.id: fixture for fixture in fixtures}
        fixtures = [by_id[fid] for fid in ordered_ids if fid in by_id]
        if not fixtures:
            self._heartbeat(target_date, 39, "RESCORE V8: candidatos ya no operativos.")
            self.stdout.write("[rescore_batch] no future candidates to rescore")
            return

        self._heartbeat(target_date, 27, f"RESCORE V8: precargando estadísticas de {len(fixtures)} partidos.")
        self.stdout.write(f"[rescore_batch] preloading shared features for {len(fixtures)} fixtures...")
        preload_started = perf_counter()
        preloader = BatchFeatureEngineeringService(fixtures, progress=lambda message: self.stdout.write(message))
        preloader.preload()
        # Build FeatureVectors once in the main thread. Workers only execute the
        # same ScoreEngineV8 logic concurrently; no Premium threshold is changed.
        work_items = [(fixture, preloader.build(fixture)) for fixture in fixtures]
        preload_seconds = perf_counter() - preload_started

        workers = self._worker_count(len(work_items), interactive)
        self._heartbeat(target_date, 31, f"RESCORE V8: evaluando {len(work_items)} partidos en {workers} workers.")
        self.stdout.write(f"[rescore_batch] parallel evaluation workers={workers}")
        evaluation_started = perf_counter()
        evaluated_rows = []
        total = len(work_items)
        completed = 0

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="v8-rescore") as executor:
            futures = {
                executor.submit(self._evaluate_one, fixture, features): fixture.id
                for fixture, features in work_items
            }
            for future in as_completed(futures):
                fixture, result = future.result()
                evaluated_rows.extend((fixture, evaluation) for evaluation in result.values())
                completed += 1
                if completed == 1 or completed % 3 == 0 or completed == total:
                    pct = 31 + int((completed / total) * 6)
                    self._heartbeat(target_date, pct, f"RESCORE V8: evaluados {completed}/{total} partidos · {workers} workers.")
                    self.stdout.write(f"[rescore_batch] evaluated {completed}/{total}")
        evaluation_seconds = perf_counter() - evaluation_started

        self._heartbeat(target_date, 38, "RESCORE V8: guardando resultados recalculados.")
        persist_started = perf_counter()
        created, updated = self._bulk_persist(evaluated_rows)
        persist_seconds = perf_counter() - persist_started
        total_seconds = perf_counter() - total_started
        self._heartbeat(target_date, 39, f"RESCORE V8 completado en {total_seconds:.0f}s; continuando con Deep Analysis.")
        self.stdout.write(self.style.SUCCESS(
            f"[rescore_batch] complete: {len(fixtures)} fixtures; created={created}, updated={updated}; "
            f"workers={workers} pool={pool_seconds:.2f}s preload={preload_seconds:.2f}s "
            f"eval={evaluation_seconds:.2f}s db={persist_seconds:.2f}s total={total_seconds:.2f}s"))
