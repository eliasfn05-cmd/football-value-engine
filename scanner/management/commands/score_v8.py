from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from engine.batch_features import BatchFeatureEngineeringService
from engine.competition_quality import classify_competition
from engine.models import Fixture, FixtureScoreState, Prediction
from engine.score_v8 import ScoreEngineV8, V8_MODEL_VERSION


PERSIST_BATCH_SIZE = 500
STATE_BATCH_SIZE = 1000


class Command(BaseCommand):
    help = "Evaluate and persist V8 BTTS/Over 2.5 predictions from PostgreSQL features."

    def add_arguments(self, parser):
        parser.add_argument("--fixture-id", dest="fixture_id")
        parser.add_argument("--date", dest="target_date", help="YYYY-MM-DD")
        parser.add_argument("--premium-only", action="store_true")
        parser.add_argument("--summary-only", action="store_true", help="Emit only summary/premium rows for batch runs.")
        parser.add_argument(
            "--force",
            action="store_true",
            help="Ignore Sprint 5 feature fingerprints and recalculate the complete date.",
        )

    @staticmethod
    def _decimal(value, places: int):
        if value is None:
            return None
        quantum = Decimal("1").scaleb(-places)
        return Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)

    @classmethod
    def _prediction_defaults(cls, evaluation):
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

    @staticmethod
    def _feature_fingerprint(fixture: Fixture, features) -> str:
        payload = {
            "model_version": V8_MODEL_VERSION,
            "fixture": {
                "external_id": fixture.external_id,
                "kickoff": fixture.kickoff.isoformat(),
                "status": fixture.status,
                "competition_ref_id": fixture.competition_ref_id,
                "season": fixture.season,
                "round": fixture.round,
                "home_team_id": fixture.home_team_id,
                "away_team_id": fixture.away_team_id,
            },
            "features": features.to_dict(),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _premium_diagnostics(predictions, engine: ScoreEngineV8) -> dict:
        counts = {
            "total": 0,
            "missing_odds": 0,
            "below_probability": 0,
            "below_edge": 0,
            "below_ev": 0,
            "below_score": 0,
            "gate_failures": 0,
        }
        near: list[dict] = []
        for pred in predictions:
            counts["total"] += 1
            reasons = pred.reasons or {}
            gates_passed = bool(reasons.get("v8_gates_passed"))
            if not gates_passed:
                counts["gate_failures"] += 1

            probability_floor = (
                engine.core.min_btts_probability
                if pred.market == "BTTS"
                else engine.core.min_over25_probability
            )
            if float(pred.probability) < probability_floor:
                counts["below_probability"] += 1
            if pred.market_odds is None:
                counts["missing_odds"] += 1
            if pred.edge is None or float(pred.edge) < engine.core.min_edge:
                counts["below_edge"] += 1
            if pred.expected_value is None or float(pred.expected_value) < engine.core.min_ev:
                counts["below_ev"] += 1
            if float(pred.score) < engine.core.min_score:
                counts["below_score"] += 1

            if pred.tier != "TIER_A":
                near.append({
                    "fixture_id": pred.fixture.external_id,
                    "match": f"{pred.fixture.home_team.name} vs {pred.fixture.away_team.name}",
                    "market": pred.market,
                    "probability": float(pred.probability),
                    "market_odds": float(pred.market_odds) if pred.market_odds is not None else None,
                    "edge": float(pred.edge) if pred.edge is not None else None,
                    "ev": float(pred.expected_value) if pred.expected_value is not None else None,
                    "score": float(pred.score),
                    "gate_failures": reasons.get("v8_gate_failures", []),
                })

        near.sort(
            key=lambda row: (
                row["score"],
                row["ev"] if row["ev"] is not None else -999,
                row["probability"],
            ),
            reverse=True,
        )
        return {"rejections": counts, "nearest": near[:5]}

    @staticmethod
    def _remove_excluded_fixture_state(fixtures: list[Fixture]) -> int:
        excluded_ids = [fixture.id for fixture in fixtures if classify_competition(fixture).excluded]
        if not excluded_ids:
            return 0
        Prediction.objects.filter(model_version=V8_MODEL_VERSION, fixture_id__in=excluded_ids).delete()
        FixtureScoreState.objects.filter(model_version=V8_MODEL_VERSION, fixture_id__in=excluded_ids).delete()
        return len(excluded_ids)

    def handle(self, *args, **options):
        fixture_id = options.get("fixture_id")
        raw_date = options.get("target_date")
        premium_only = bool(options.get("premium_only"))
        summary_only = bool(options.get("summary_only"))
        force = bool(options.get("force"))
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
            competition_quality = classify_competition(fixture)
            if competition_quality.excluded:
                Prediction.objects.filter(fixture=fixture, model_version=V8_MODEL_VERSION).delete()
                FixtureScoreState.objects.filter(fixture=fixture, model_version=V8_MODEL_VERSION).delete()
                self.stdout.write(
                    json.dumps(
                        {
                            "fixture_id": fixture.external_id,
                            "excluded": True,
                            "reason": competition_quality.reason,
                            "competition_quality": competition_quality.label,
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                )
                return
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
        all_fixtures = list(
            Fixture.objects.select_related("home_team", "away_team", "competition_ref")
            .filter(kickoff__gte=start, kickoff__lt=end)
            .order_by("kickoff")
        )
        excluded_count = self._remove_excluded_fixture_state(all_fixtures)
        fixtures = [fixture for fixture in all_fixtures if not classify_competition(fixture).excluded]

        incremental = summary_only and not force
        self.stdout.write(
            f"[score_v8] fixtures={len(fixtures)} excluded_competition={excluded_count} "
            f"incremental={str(incremental).lower()}; preloading batch features...",
            ending="\n",
        )
        preloader = BatchFeatureEngineeringService(
            fixtures,
            progress=lambda message: self.stdout.write(message, ending="\n"),
        )
        preloader.preload()
        self.stdout.write("[score_v8] feature preload complete; evaluating in memory...", ending="\n")

        fixture_ids = [f.id for f in fixtures]
        existing_qs = Prediction.objects.filter(model_version=V8_MODEL_VERSION, fixture_id__in=fixture_ids).order_by("id")
        existing: dict[tuple[int, str, str], Prediction] = {}
        for pred in existing_qs.iterator(chunk_size=2000):
            existing.setdefault((pred.fixture_id, pred.market, pred.selection), pred)

        score_states = {
            state.fixture_id: state
            for state in FixtureScoreState.objects.filter(
                model_version=V8_MODEL_VERSION,
                fixture_id__in=fixture_ids,
            ).iterator(chunk_size=2000)
        } if incremental else {}

        to_create: list[Prediction] = []
        to_update: list[Prediction] = []
        state_create: list[FixtureScoreState] = []
        state_update: list[FixtureScoreState] = []
        unchanged_predictions = 0
        skipped_fixtures = 0
        evaluated_fixtures = 0
        rows: list[dict] = []

        for idx, fixture in enumerate(fixtures, start=1):
            features = preloader.build(fixture)
            fingerprint = self._feature_fingerprint(fixture, features)
            state = score_states.get(fixture.id)
            if incremental and state is not None and state.feature_fingerprint == fingerprint:
                skipped_fixtures += 1
                if idx == 1 or idx % 100 == 0 or idx == len(fixtures):
                    self.stdout.write(
                        f"[score_v8] inspected {idx}/{len(fixtures)} evaluated={evaluated_fixtures} skipped={skipped_fixtures}",
                        ending="\n",
                    )
                continue

            evaluated_fixtures += 1
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
                    unchanged_predictions += 1

            if state is None:
                state_create.append(
                    FixtureScoreState(
                        fixture=fixture,
                        model_version=V8_MODEL_VERSION,
                        feature_fingerprint=fingerprint,
                    )
                )
            else:
                state.feature_fingerprint = fingerprint
                state_update.append(state)

            if not summary_only:
                row = self._fixture_payload(fixture, result, premium_only)
                if row is not None:
                    rows.append(row)

            if idx == 1 or idx % 100 == 0 or idx == len(fixtures):
                self.stdout.write(
                    f"[score_v8] inspected {idx}/{len(fixtures)} evaluated={evaluated_fixtures} skipped={skipped_fixtures}",
                    ending="\n",
                )

        update_fields = [
            "probability", "fair_odds", "market_odds", "edge",
            "expected_value", "score", "tier", "reasons",
        ]
        self.stdout.write(
            f"[score_v8] persist plan create={len(to_create)} update={len(to_update)} "
            f"unchanged_predictions={unchanged_predictions} state_create={len(state_create)} "
            f"state_update={len(state_update)} batch_size={PERSIST_BATCH_SIZE}",
            ending="\n",
        )

        created_done = 0
        for offset in range(0, len(to_create), PERSIST_BATCH_SIZE):
            batch = to_create[offset: offset + PERSIST_BATCH_SIZE]
            with transaction.atomic():
                Prediction.objects.bulk_create(batch, batch_size=PERSIST_BATCH_SIZE)
            created_done += len(batch)
            self.stdout.write(f"[score_v8] persisted creates {created_done}/{len(to_create)}", ending="\n")

        updated_done = 0
        for offset in range(0, len(to_update), PERSIST_BATCH_SIZE):
            batch = to_update[offset: offset + PERSIST_BATCH_SIZE]
            with transaction.atomic():
                Prediction.objects.bulk_update(batch, update_fields, batch_size=PERSIST_BATCH_SIZE)
            updated_done += len(batch)
            self.stdout.write(f"[score_v8] persisted updates {updated_done}/{len(to_update)}", ending="\n")

        if state_create:
            FixtureScoreState.objects.bulk_create(state_create, batch_size=STATE_BATCH_SIZE, ignore_conflicts=True)
        if state_update:
            FixtureScoreState.objects.bulk_update(
                state_update,
                ["feature_fingerprint", "scored_at"],
                batch_size=STATE_BATCH_SIZE,
            )

        day_predictions = list(
            Prediction.objects.select_related("fixture__home_team", "fixture__away_team", "fixture__competition_ref")
            .filter(
                model_version=V8_MODEL_VERSION,
                fixture__kickoff__gte=start,
                fixture__kickoff__lt=end,
            )
            .order_by("-score")
        )
        day_predictions = [pred for pred in day_predictions if not classify_competition(pred.fixture).excluded]
        premium_predictions = [pred for pred in day_predictions if pred.tier == "TIER_A"]
        premium = [
            {
                "fixture_id": pred.fixture.external_id,
                "match": f"{pred.fixture.home_team.name} vs {pred.fixture.away_team.name}",
                "market": pred.market,
                "selection": pred.selection,
                "probability": pred.probability,
                "market_odds": pred.market_odds,
                "fair_odds": pred.fair_odds,
                "edge": pred.edge,
                "expected_value": pred.expected_value,
                "score": pred.score,
                "tier": pred.tier,
                "gate_failures": (pred.reasons or {}).get("v8_gate_failures", []),
            }
            for pred in sorted(
                premium_predictions,
                key=lambda pred: (
                    float(pred.expected_value) if pred.expected_value is not None else -999,
                    float(pred.score),
                ),
                reverse=True,
            )
        ]
        diagnostics = self._premium_diagnostics(day_predictions, engine)
        self.stdout.write(
            f"[score_v8] premium diagnostics {json.dumps(diagnostics['rejections'], sort_keys=True)}",
            ending="\n",
        )
        if not premium:
            self.stdout.write(
                f"[score_v8] nearest premium {json.dumps(diagnostics['nearest'], ensure_ascii=False, default=str)}",
                ending="\n",
            )

        payload = {
            "date": raw_date,
            "model_version": V8_MODEL_VERSION,
            "fixtures_total": len(fixtures),
            "fixtures_excluded_competition": excluded_count,
            "fixtures_evaluated": evaluated_fixtures,
            "fixtures_skipped_incremental": skipped_fixtures,
            "premium_count": len(premium),
            "premium": premium,
            "premium_diagnostics": diagnostics,
            "fixtures": None if (premium_only or summary_only) else rows,
        }
        self.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        self.stdout.write(
            self.style.SUCCESS(
                f"[score_v8] complete total={len(fixtures)} excluded={excluded_count} evaluated={evaluated_fixtures} "
                f"skipped={skipped_fixtures} premium={len(premium)} created={len(to_create)} "
                f"updated={len(to_update)} unchanged_predictions={unchanged_predictions}"
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
