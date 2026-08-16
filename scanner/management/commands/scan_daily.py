from __future__ import annotations

import json
import os
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from engine.batch_features import BatchFeatureEngineeringService
from engine.competition_quality import classify_competition
from engine.models import Fixture, Prediction
from engine.score_v8 import ScoreEngineV8, V8_MODEL_VERSION
from engine.time_window import window_bounds, window_label
from scanner.providers.api_football import APIFootballProvider
from scanner.service import DailyScanner


PERSIST_BATCH_SIZE = 500
DEFAULT_BOOTSTRAP_BATCH_SIZE = 100


class Command(BaseCommand):
    help = "Scan one date or run a fast future-only V8 bootstrap for interactive Premium generation."

    def add_arguments(self, parser):
        parser.add_argument("--date", dest="target_date", help="YYYY-MM-DD. Defaults to APP_TIMEZONE local date.")
        parser.add_argument(
            "--v8-bootstrap",
            action="store_true",
            help="Score only future, non-excluded fixtures with shared batch preloads.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Maximum future fixtures for --v8-bootstrap. 0 = all fixtures in the selected kickoff window.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=DEFAULT_BOOTSTRAP_BATCH_SIZE,
            help=f"Fixtures per V8 preload/evaluation batch (default: {DEFAULT_BOOTSTRAP_BATCH_SIZE}).",
        )

    @staticmethod
    def _decimal(value, places: int):
        if value is None:
            return None
        quantum = Decimal("1").scaleb(-places)
        return Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)

    @classmethod
    def _defaults(cls, evaluation):
        return {
            "probability": cls._decimal(evaluation["probability"], 5),
            "fair_odds": cls._decimal(evaluation["fair_odds"], 3),
            "market_odds": cls._decimal(evaluation["market_odds"], 3),
            "edge": cls._decimal(evaluation["edge"], 5),
            "expected_value": cls._decimal(evaluation["expected_value"], 5),
            "score": cls._decimal(evaluation["score"], 2),
            "tier": evaluation["tier"],
            "reasons": evaluation["reasons"],
        }

    @staticmethod
    def _prediction_changed(pred: Prediction, defaults: dict) -> bool:
        return any(getattr(pred, field) != value for field, value in defaults.items())

    @classmethod
    def _bulk_persist_evaluations(cls, evaluated_rows: list[tuple[Fixture, dict]]) -> tuple[int, int, int]:
        """Persist unchanged V8 evaluations with bounded remote DB round trips."""
        if not evaluated_rows:
            return 0, 0, 0

        fixture_ids = {fixture.id for fixture, _evaluation in evaluated_rows}
        existing: dict[tuple[int, str, str], Prediction] = {}
        existing_qs = Prediction.objects.filter(
            model_version=V8_MODEL_VERSION,
            fixture_id__in=fixture_ids,
        ).order_by("id")
        for pred in existing_qs.iterator(chunk_size=2000):
            existing.setdefault((pred.fixture_id, pred.market, pred.selection), pred)

        to_create: list[Prediction] = []
        to_update: list[Prediction] = []
        unchanged = 0
        for fixture, evaluation in evaluated_rows:
            key = (fixture.id, evaluation["market"], evaluation["selection"])
            defaults = cls._defaults(evaluation)
            pred = existing.get(key)
            if pred is None:
                pred = Prediction(
                    fixture=fixture,
                    model_version=V8_MODEL_VERSION,
                    market=evaluation["market"],
                    selection=evaluation["selection"],
                    **defaults,
                )
                to_create.append(pred)
                existing[key] = pred
            elif cls._prediction_changed(pred, defaults):
                for field, value in defaults.items():
                    setattr(pred, field, value)
                to_update.append(pred)
            else:
                unchanged += 1

        update_fields = [
            "probability", "fair_odds", "market_odds", "edge",
            "expected_value", "score", "tier", "reasons",
        ]
        with transaction.atomic():
            if to_create:
                Prediction.objects.bulk_create(to_create, batch_size=PERSIST_BATCH_SIZE)
            if to_update:
                Prediction.objects.bulk_update(to_update, update_fields, batch_size=PERSIST_BATCH_SIZE)

        return len(to_create), len(to_update), unchanged

    @staticmethod
    def _chunks(items: list[Fixture], batch_size: int):
        for offset in range(0, len(items), batch_size):
            yield offset // batch_size + 1, items[offset: offset + batch_size]

    def _bootstrap_v8(self, target_date: date, *, limit: int, batch_size: int) -> None:
        start, end = window_bounds(target_date)
        from django.utils import timezone
        future_start = max(start, timezone.now())

        queryset = (
            Fixture.objects.select_related("home_team", "away_team", "competition_ref")
            .filter(kickoff__gte=future_start, kickoff__lt=end)
            .order_by("kickoff", "id")
        )
        if limit > 0:
            raw_fixtures = list(queryset[:limit])
        else:
            raw_fixtures = list(queryset)

        fixtures = [fixture for fixture in raw_fixtures if not classify_competition(fixture).excluded]
        if not fixtures:
            self.stdout.write(
                f"[v8-bootstrap] no future professional fixtures to score in window={window_label()}"
            )
            return

        batch_size = max(10, int(batch_size or DEFAULT_BOOTSTRAP_BATCH_SIZE))
        total_batches = (len(fixtures) + batch_size - 1) // batch_size
        self.stdout.write(
            f"[v8-bootstrap] window={window_label()} discovered={len(raw_fixtures)} "
            f"professional={len(fixtures)} batches={total_batches} batch_size={batch_size}; "
            "full-window coverage enabled.",
            ending="\n",
        )

        engine = ScoreEngineV8()
        evaluated = 0
        created_total = 0
        updated_total = 0
        unchanged_total = 0

        for batch_no, batch in self._chunks(fixtures, batch_size):
            self.stdout.write(
                f"[v8-bootstrap] batch {batch_no}/{total_batches} preload fixtures={len(batch)}...",
                ending="\n",
            )
            preloader = BatchFeatureEngineeringService(
                batch,
                progress=lambda message: self.stdout.write(message, ending="\n"),
            )
            preloader.preload()

            evaluated_rows: list[tuple[Fixture, dict]] = []
            for fixture in batch:
                features = preloader.build(fixture)
                result = engine.evaluate(fixture, features)
                evaluated += 1
                for evaluation in result.values():
                    evaluated_rows.append((fixture, evaluation))

            created, updated, unchanged = self._bulk_persist_evaluations(evaluated_rows)
            created_total += created
            updated_total += updated
            unchanged_total += unchanged
            self.stdout.write(
                f"[v8-bootstrap] batch {batch_no}/{total_batches} complete; "
                f"evaluated_total={evaluated}/{len(fixtures)} rows={len(evaluated_rows)} "
                f"created={created} updated={updated} unchanged={unchanged}",
                ending="\n",
            )

        tier_a = Prediction.objects.filter(
            model_version=V8_MODEL_VERSION,
            fixture__kickoff__gte=future_start,
            fixture__kickoff__lt=end,
            tier="TIER_A",
        ).count()
        payload = {
            "date": target_date.isoformat(),
            "window": window_label(),
            "model_version": V8_MODEL_VERSION,
            "future_discovered_fixtures": len(raw_fixtures),
            "future_professional_fixtures": len(fixtures),
            "fixtures_evaluated": evaluated,
            "batches": total_batches,
            "batch_size": batch_size,
            "predictions_created": created_total,
            "predictions_updated": updated_total,
            "predictions_unchanged": unchanged_total,
            "raw_tier_a": tier_a,
        }
        self.stdout.write(json.dumps(payload, ensure_ascii=False, default=str))
        self.stdout.write(self.style.SUCCESS(
            f"Batched V8 bootstrap complete: {evaluated}/{len(fixtures)} professional fixtures "
            f"evaluated in {window_label()}, raw Tier A={tier_a}."
        ))

    def handle(self, *args, **options):
        timezone_name = os.getenv("APP_TIMEZONE", "America/Lima")
        raw_date = options.get("target_date")
        try:
            target_date = datetime.fromisoformat(raw_date).date() if raw_date else datetime.now(ZoneInfo(timezone_name)).date()
        except (ValueError, KeyError) as exc:
            raise CommandError("Invalid --date or APP_TIMEZONE") from exc

        if options.get("v8_bootstrap"):
            self._bootstrap_v8(
                target_date,
                limit=int(options.get("limit") or 0),
                batch_size=int(options.get("batch_size") or DEFAULT_BOOTSTRAP_BATCH_SIZE),
            )
            return

        try:
            provider = APIFootballProvider()
            report = DailyScanner(provider).scan_date(target_date)
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        report["app_timezone"] = timezone_name
        self.stdout.write(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        self.stdout.write(self.style.SUCCESS(
            f"Scanned {report['fixtures_scanned']} fixtures; Betano coverage {report['coverage_betano_pct']}%; "
            f"Tier A selections: {len(report['tier_a'])}"
        ))