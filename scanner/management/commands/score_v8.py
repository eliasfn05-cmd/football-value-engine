from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from engine.batch_features import BatchFeatureEngineeringService
from engine.models import Fixture, Prediction
from engine.score_v8 import ScoreEngineV8, V8_MODEL_VERSION


# 500 keeps PostgreSQL statements bounded while cutting round trips versus the
# previous 200-row batches. More importantly, unchanged rows never reach this
# persistence path at all.
PERSIST_BATCH_SIZE = 500


class Command(BaseCommand):
    help = "Evaluate and persist V8 BTTS/Over 2.5 predictions from PostgreSQL features."

    def add_arguments(self, parser):
        parser.add_argument("--fixture-id", dest="fixture_id")
        parser.add_argument("--date", dest="target_date", help="YYYY-MM-DD")
        parser.add_argument("--premium-only", action="store_true")
        parser.add_argument("--summary-only", action="store_true", help="Emit only summary/premium rows for batch runs.")

    @staticmethod
    def _decimal(value, places: int):
        if value is None:
            return None
        quantum = Decimal("1").scaleb(-places)
        return Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)

    @classmethod
    def _prediction_defaults(cls, evaluation):
        """Normalize values exactly as Prediction DecimalFields store them.

        Comparing normalized values lets repeated daily/refresh runs skip rows
        whose persisted representation is already identical.
        """
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

    def handle(self, *args, **options):
        fixture_id = options.get("fixture_id")
        raw_date = options.get("target_date")
        premium_only = bool(options.get("premium_only"))
        summary_only = bool(options.get("summary_only"))
        if not fixture_id and not raw_date:
            raise CommandError("Provide --fixture-id or --date")
        if fixture_id and raw_date:
            raise CommandError("Use only one of --fixture-id or --date")

        engine = ScoreEngineV8()

        if fixture_id:
            fixture = (
                Fixture.objects.select_related("home_team", "away_team", "competition_ref")
                .filter(external_id=str(fixture_id))
                .first()
            )
            if not fixture:
                raise CommandError(f"Fixture {fixture_id} not found")
            result = engine.evaluate_and_persist(fixture)
            payload = self._fixture_payload(fixture, result, premium_only)
            self.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
            return

        try:
            target_date = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise CommandError("--date must use YYYY-MM-DD") from exc

        start = timezone.make_aware(datetime.combine(target_date, time.min))
        end = start + timedelta(days=1)
        fixtures = list(
            Fixture.objects.select_related("home_team", "away_team", "competition_ref")
            .filter(kickoff__gte=start, kickoff__lt=end)
            .order_by("kickoff")
        )

        self.stdout.write(f"[score_v8] fixtures={len(fixtures)}; preloading batch features...", ending="\n")
        preloader = BatchFeatureEngineeringService(fixtures)
        preloader.preload()
        self.stdout.write("[score_v8] feature preload complete; evaluating in memory...", ending="\n")

        fixture_ids = [f.id for f in fixtures]
        existing_qs = Prediction.objects.filter(model_version=V8_MODEL_VERSION, fixture_id__in=fixture_ids).order_by("id")
        existing: dict[tuple[int, str, str], Prediction] = {}
        for pred in existing_qs.iterator(chunk_size=2000):
            existing.setdefault((pred.fixture_id, pred.market, pred.selection), pred)

        to_create: list[Prediction] = []
        to_update: list[Prediction] = []
        unchanged = 0
        premium: list[dict] = []
        rows: list[dict] = []

        for idx, fixture in enumerate(fixtures, start=1):
            features = preloader.build(fixture)
            result = engine.evaluate(fixture, features)

            for evaluation in result.values():
                key = (fixture.id, evaluation["market"], evaluation["selection"])
                defaults = self._prediction_defaults(evaluation)
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
                elif self._prediction_changed(pred, defaults):
                    for field, value in defaults.items():
                        setattr(pred, field, value)
                    to_update.append(pred)
                else:
                    unchanged += 1

                if evaluation["tier"] == "TIER_A":
                    premium.append({
                        "fixture_id": fixture.external_id,
                        "match": f"{fixture.home_team.name} vs {fixture.away_team.name}",
                        **self._market_payload(evaluation),
                    })

            if not summary_only:
                row = self._fixture_payload(fixture, result, premium_only)
                if row is not None:
                    rows.append(row)

            if idx == 1 or idx % 100 == 0 or idx == len(fixtures):
                self.stdout.write(f"[score_v8] evaluated {idx}/{len(fixtures)}", ending="\n")

        update_fields = [
            "probability", "fair_odds", "market_odds", "edge",
            "expected_value", "score", "tier", "reasons",
        ]
        self.stdout.write(
            f"[score_v8] persist plan create={len(to_create)} update={len(to_update)} "
            f"unchanged={unchanged} batch_size={PERSIST_BATCH_SIZE}",
            ending="\n",
        )

        created_done = 0
        for offset in range(0, len(to_create), PERSIST_BATCH_SIZE):
            batch = to_create[offset: offset + PERSIST_BATCH_SIZE]
            with transaction.atomic():
                Prediction.objects.bulk_create(batch, batch_size=PERSIST_BATCH_SIZE)
            created_done += len(batch)
            self.stdout.write(
                f"[score_v8] persisted creates {created_done}/{len(to_create)}",
                ending="\n",
            )

        updated_done = 0
        for offset in range(0, len(to_update), PERSIST_BATCH_SIZE):
            batch = to_update[offset: offset + PERSIST_BATCH_SIZE]
            with transaction.atomic():
                Prediction.objects.bulk_update(batch, update_fields, batch_size=PERSIST_BATCH_SIZE)
            updated_done += len(batch)
            self.stdout.write(
                f"[score_v8] persisted updates {updated_done}/{len(to_update)}",
                ending="\n",
            )

        premium.sort(key=lambda item: ((item.get("expected_value") or -999), item.get("score") or 0), reverse=True)
        payload = {
            "date": raw_date,
            "model_version": V8_MODEL_VERSION,
            "fixtures_scored": len(fixtures),
            "premium_count": len(premium),
            "premium": premium,
            "fixtures": None if (premium_only or summary_only) else rows,
        }
        self.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        self.stdout.write(
            self.style.SUCCESS(
                f"[score_v8] complete fixtures={len(fixtures)} premium={len(premium)} "
                f"created={len(to_create)} updated={len(to_update)} unchanged={unchanged}"
            )
        )

    @staticmethod
    def _market_payload(evaluation):
        return {
            "market": evaluation["market"],
            "selection": evaluation["selection"],
            "probability": evaluation["probability"],
            "market_odds": evaluation["market_odds"],
            "fair_odds": evaluation["fair_odds"],
            "edge": evaluation["edge"],
            "expected_value": evaluation["expected_value"],
            "score": evaluation["score"],
            "tier": evaluation["tier"],
            "gate_failures": evaluation["reasons"].get("v8_gate_failures", []),
        }

    @classmethod
    def _fixture_payload(cls, fixture, result, premium_only):
        markets = []
        for evaluation in result.values():
            if premium_only and evaluation["tier"] != "TIER_A":
                continue
            markets.append(cls._market_payload(evaluation))
        if premium_only and not markets:
            return None
        return {
            "fixture_id": fixture.external_id,
            "match": f"{fixture.home_team.name} vs {fixture.away_team.name}",
            "kickoff": fixture.kickoff.isoformat(),
            "markets": markets,
        }
