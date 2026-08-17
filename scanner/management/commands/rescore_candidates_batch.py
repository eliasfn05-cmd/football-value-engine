from __future__ import annotations

from datetime import date
from time import perf_counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from engine.batch_features import BatchFeatureEngineeringService
from engine.candidate_pool import CandidatePoolRule, high_recall_candidate_pool
from engine.models import Fixture, Prediction
from engine.score_v8 import ScoreEngineV8, V8_MODEL_VERSION


PERSIST_BATCH_SIZE = 500


class Command(BaseCommand):
    help = "Batch-rescore the Premium candidate pool using one shared feature preload."

    def add_arguments(self, parser):
        parser.add_argument("--date", dest="target_date", required=True, help="YYYY-MM-DD")
        parser.add_argument("--limit", type=int, default=40)
        parser.add_argument(
            "--fixture-ids",
            default="",
            help="Comma-separated preselected fixture ids. Avoids rebuilding the candidate pool.",
        )

    def _bulk_persist(self, evaluated_rows: list[tuple[Fixture, dict]]) -> tuple[int, int]:
        if not evaluated_rows:
            return 0, 0

        fixture_ids = {fixture.id for fixture, _evaluation in evaluated_rows}
        existing_rows = Prediction.objects.filter(
            fixture_id__in=fixture_ids,
            model_version=V8_MODEL_VERSION,
        )
        existing = {(row.fixture_id, row.market, row.selection): row for row in existing_rows}

        to_create: list[Prediction] = []
        to_update: list[Prediction] = []
        update_fields = [
            "probability", "fair_odds", "market_odds", "edge",
            "expected_value", "score", "tier", "reasons",
        ]

        for fixture, evaluation in evaluated_rows:
            key = (fixture.id, evaluation["market"], evaluation["selection"])
            row = existing.get(key)
            values = {
                "probability": evaluation["probability"],
                "fair_odds": evaluation["fair_odds"],
                "market_odds": evaluation["market_odds"],
                "edge": evaluation["edge"],
                "expected_value": evaluation["expected_value"],
                "score": evaluation["score"],
                "tier": evaluation["tier"],
                "reasons": evaluation["reasons"],
            }
            if row is None:
                to_create.append(Prediction(
                    fixture=fixture,
                    model_version=V8_MODEL_VERSION,
                    market=evaluation["market"],
                    selection=evaluation["selection"],
                    **values,
                ))
                continue
            for field, value in values.items():
                setattr(row, field, value)
            to_update.append(row)

        with transaction.atomic():
            if to_create:
                Prediction.objects.bulk_create(to_create, batch_size=PERSIST_BATCH_SIZE)
            if to_update:
                Prediction.objects.bulk_update(to_update, update_fields, batch_size=PERSIST_BATCH_SIZE)
        return len(to_create), len(to_update)

    @staticmethod
    def _parse_fixture_ids(raw: str, limit: int) -> list[int]:
        if not raw.strip():
            return []
        ordered: list[int] = []
        seen: set[int] = set()
        for token in raw.split(","):
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

    def handle(self, *args, **options):
        total_started = perf_counter()
        try:
            target_date = date.fromisoformat(options["target_date"])
        except ValueError as exc:
            raise CommandError("--date must use YYYY-MM-DD") from exc

        limit = max(1, min(int(options.get("limit") or 40), 60))
        supplied_ids = self._parse_fixture_ids(options.get("fixture_ids") or "", limit)

        if supplied_ids:
            ordered_ids = supplied_ids
            pool_seconds = 0.0
            self.stdout.write(f"[rescore_batch] reusing {len(ordered_ids)} preselected fixture ids; pool rebuild skipped")
        else:
            pool_started = perf_counter()
            pool = high_recall_candidate_pool(target_date, rule=CandidatePoolRule(limit=limit))
            pool_seconds = perf_counter() - pool_started
            ordered_ids = [entry.fixture_id for entry in pool]

        if not ordered_ids:
            self.stdout.write("[rescore_batch] no candidates to rescore")
            return

        fixtures = list(
            Fixture.objects.filter(id__in=ordered_ids, kickoff__gt=timezone.now())
            .select_related("home_team", "away_team", "competition_ref")
        )
        by_id = {fixture.id: fixture for fixture in fixtures}
        fixtures = [by_id[fid] for fid in ordered_ids if fid in by_id]
        if not fixtures:
            self.stdout.write("[rescore_batch] no future candidates to rescore")
            return

        self.stdout.write(
            f"[rescore_batch] pool={len(ordered_ids)} in {pool_seconds:.2f}s; "
            f"preloading shared features for {len(fixtures)} candidate fixtures..."
        )
        preload_started = perf_counter()
        preloader = BatchFeatureEngineeringService(fixtures, progress=lambda message: self.stdout.write(message))
        preloader.preload()
        preload_seconds = perf_counter() - preload_started
        self.stdout.write(f"[rescore_batch] preload complete in {preload_seconds:.2f}s")

        engine = ScoreEngineV8()
        evaluation_started = perf_counter()
        evaluated_rows: list[tuple[Fixture, dict]] = []
        for index, fixture in enumerate(fixtures, start=1):
            features = preloader.build(fixture)
            result = engine.evaluate(fixture, features)
            for evaluation in result.values():
                evaluated_rows.append((fixture, evaluation))
            if index == 1 or index % 10 == 0 or index == len(fixtures):
                self.stdout.write(f"[rescore_batch] evaluated {index}/{len(fixtures)}")

        evaluation_seconds = perf_counter() - evaluation_started
        self.stdout.write(
            f"[rescore_batch] evaluation complete in {evaluation_seconds:.2f}s; "
            f"persisting {len(evaluated_rows)} predictions in bulk..."
        )
        persist_started = perf_counter()
        created, updated = self._bulk_persist(evaluated_rows)
        persist_seconds = perf_counter() - persist_started
        total_seconds = perf_counter() - total_started

        self.stdout.write(self.style.SUCCESS(
            f"[rescore_batch] complete: {len(fixtures)} fixtures; created={created}, updated={updated}; "
            f"pool={pool_seconds:.2f}s preload={preload_seconds:.2f}s eval={evaluation_seconds:.2f}s "
            f"db={persist_seconds:.2f}s total={total_seconds:.2f}s"
        ))
