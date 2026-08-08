from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from statistics import mean
from typing import Iterable

from django.db import transaction
from django.utils import timezone

from engine.models import Prediction
from .models import LearningSnapshot, PredictionOutcome


SETTLEMENT_BATCH_SIZE = 1000
LEARNING_BATCH_SIZE = 500


@dataclass(frozen=True)
class MetricSummary:
    scope: str
    sample_size: int
    wins: int
    losses: int
    voids: int
    win_rate: float | None
    total_profit_units: float
    roi: float | None
    yield_pct: float | None
    avg_probability: float | None
    avg_edge: float | None
    avg_expected_value: float | None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class SettlementService:
    """Settle stored predictions using only the recorded final score.

    Settlement never changes the original prediction, odds or reasons. This
    preserves a clean pre-match audit trail for backtesting.
    """

    @staticmethod
    def _result_for(prediction: Prediction, home_goals: int, away_goals: int) -> tuple[str, str]:
        market = prediction.market.upper()
        selection = prediction.selection.upper()

        if market == "BTTS" and selection in {"YES", "SI", "SÍ"}:
            won = home_goals > 0 and away_goals > 0
            return (PredictionOutcome.RESULT_WIN if won else PredictionOutcome.RESULT_LOSS, "btts_yes")

        if market in {"OVER_2_5", "OVER 2.5", "OVER25"} and selection in {"OVER", "OVER_2_5", "OVER 2.5"}:
            won = home_goals + away_goals >= 3
            return (PredictionOutcome.RESULT_WIN if won else PredictionOutcome.RESULT_LOSS, "over_2_5")

        return PredictionOutcome.RESULT_VOID, "unsupported_market"

    @staticmethod
    def _profit(prediction: Prediction, result: str, stake: Decimal) -> Decimal:
        if result == PredictionOutcome.RESULT_VOID:
            return Decimal("0")
        if result == PredictionOutcome.RESULT_LOSS:
            return -stake
        if prediction.market_odds is None:
            return Decimal("0")
        return (Decimal(prediction.market_odds) - Decimal("1")) * stake

    @transaction.atomic
    def settle_prediction(self, prediction: Prediction, *, stake_units: Decimal = Decimal("1")) -> PredictionOutcome | None:
        """Single-prediction compatibility path used by tests/manual inspection."""
        fixture = prediction.fixture
        if fixture.home_goals is None or fixture.away_goals is None:
            return None

        result, reason = self._result_for(prediction, fixture.home_goals, fixture.away_goals)
        profit = self._profit(prediction, result, stake_units)
        outcome, _ = PredictionOutcome.objects.update_or_create(
            prediction=prediction,
            defaults={
                "result": result,
                "home_goals": fixture.home_goals,
                "away_goals": fixture.away_goals,
                "stake_units": stake_units,
                "profit_units": profit,
                "settled_at": timezone.now(),
                "settlement_reason": reason,
            },
        )
        return outcome

    def settle_finished(self, *, model_version: str | None = None) -> dict[str, int]:
        """Bulk settle only finished predictions that are not already settled."""
        qs = Prediction.objects.select_related("fixture").filter(
            fixture__home_goals__isnull=False,
            fixture__away_goals__isnull=False,
        )
        if model_version:
            qs = qs.filter(model_version=model_version)

        predictions = list(qs.iterator(chunk_size=2000))
        if not predictions:
            return {"settled": 0, "wins": 0, "losses": 0, "voids": 0, "skipped_existing": 0}

        prediction_ids = [prediction.id for prediction in predictions]
        existing = {
            outcome.prediction_id: outcome
            for outcome in PredictionOutcome.objects.filter(prediction_id__in=prediction_ids).iterator(chunk_size=2000)
        }

        now = timezone.now()
        stake = Decimal("1")
        to_create: list[PredictionOutcome] = []
        to_update: list[PredictionOutcome] = []
        wins = losses = voids = skipped_existing = 0

        for prediction in predictions:
            previous = existing.get(prediction.id)
            if previous is not None and previous.result != PredictionOutcome.RESULT_PENDING:
                skipped_existing += 1
                continue

            fixture = prediction.fixture
            result, reason = self._result_for(prediction, fixture.home_goals, fixture.away_goals)
            profit = self._profit(prediction, result, stake)

            if result == PredictionOutcome.RESULT_WIN:
                wins += 1
            elif result == PredictionOutcome.RESULT_LOSS:
                losses += 1
            else:
                voids += 1

            if previous is None:
                to_create.append(
                    PredictionOutcome(
                        prediction=prediction,
                        result=result,
                        home_goals=fixture.home_goals,
                        away_goals=fixture.away_goals,
                        stake_units=stake,
                        profit_units=profit,
                        settled_at=now,
                        settlement_reason=reason,
                    )
                )
            else:
                previous.result = result
                previous.home_goals = fixture.home_goals
                previous.away_goals = fixture.away_goals
                previous.stake_units = stake
                previous.profit_units = profit
                previous.settled_at = now
                previous.settlement_reason = reason
                to_update.append(previous)

        if to_create:
            PredictionOutcome.objects.bulk_create(to_create, batch_size=SETTLEMENT_BATCH_SIZE, ignore_conflicts=True)
        if to_update:
            PredictionOutcome.objects.bulk_update(
                to_update,
                [
                    "result", "home_goals", "away_goals", "stake_units",
                    "profit_units", "settled_at", "settlement_reason",
                ],
                batch_size=SETTLEMENT_BATCH_SIZE,
            )

        settled = len(to_create) + len(to_update)
        return {
            "settled": settled,
            "wins": wins,
            "losses": losses,
            "voids": voids,
            "created": len(to_create),
            "updated": len(to_update),
            "skipped_existing": skipped_existing,
        }


class LearningAnalyticsService:
    """Measure model and rule performance without modifying model weights."""

    @staticmethod
    def _active_rule_scopes(prediction: Prediction) -> set[str]:
        reasons = prediction.reasons or {}
        scopes: set[str] = set()

        if reasons.get("home_clean_sheet_risk"):
            scopes.add("rule:home_clean_sheet_risk")
        if reasons.get("away_clean_sheet_risk"):
            scopes.add("rule:away_clean_sheet_risk")
        if reasons.get("matchup_suppression"):
            scopes.add("rule:matchup_suppression")
        if float(reasons.get("early_season_penalty") or 0) > 0:
            scopes.add("rule:early_season")
        if int(reasons.get("europe_congestion_teams") or 0) > 0:
            scopes.add("rule:europe_congestion")
        if reasons.get("low_pace"):
            scopes.add("rule:low_pace")
        if reasons.get("high_pace"):
            scopes.add("rule:high_pace")
        if reasons.get("tournament_draw_incentive"):
            scopes.add("rule:draw_incentive")
        if reasons.get("home_lineup_state") in {"heavy_rotation", "mild_rotation"}:
            scopes.add(f"rule:home_{reasons['home_lineup_state']}")
        if reasons.get("away_lineup_state") in {"heavy_rotation", "mild_rotation"}:
            scopes.add(f"rule:away_{reasons['away_lineup_state']}")

        home_over = reasons.get("home_over25_last5_home")
        away_over = reasons.get("away_over25_last5_away")
        home_btts = reasons.get("home_btts_last5_home")
        away_btts = reasons.get("away_btts_last5_away")
        if home_over is not None and float(home_over) <= 0.20:
            scopes.add("rule:ahpc_low_home_over")
        if away_over is not None and float(away_over) <= 0.20:
            scopes.add("rule:ahpc_low_away_over")
        if home_btts is not None and float(home_btts) <= 0.20:
            scopes.add("rule:ahpc_low_home_btts")
        if away_btts is not None and float(away_btts) <= 0.20:
            scopes.add("rule:ahpc_low_away_btts")
        return scopes

    @staticmethod
    def _summary(scope: str, outcomes: Iterable[PredictionOutcome]) -> MetricSummary:
        rows = list(outcomes)
        wins = sum(row.result == PredictionOutcome.RESULT_WIN for row in rows)
        losses = sum(row.result == PredictionOutcome.RESULT_LOSS for row in rows)
        voids = sum(row.result == PredictionOutcome.RESULT_VOID for row in rows)
        decided = wins + losses
        win_rate = wins / decided if decided else None

        priced = [row for row in rows if row.prediction.market_odds is not None and row.result != PredictionOutcome.RESULT_VOID]
        total_profit = sum((Decimal(row.profit_units) for row in priced), Decimal("0"))
        total_stake = sum((Decimal(row.stake_units) for row in priced), Decimal("0"))
        roi = float(total_profit / total_stake) if total_stake else None
        yield_pct = roi * 100 if roi is not None else None

        probabilities = [float(row.prediction.probability) for row in rows if row.prediction.probability is not None]
        edges = [float(row.prediction.edge) for row in rows if row.prediction.edge is not None]
        evs = [float(row.prediction.expected_value) for row in rows if row.prediction.expected_value is not None]

        return MetricSummary(
            scope=scope,
            sample_size=len(rows),
            wins=wins,
            losses=losses,
            voids=voids,
            win_rate=round(win_rate, 5) if win_rate is not None else None,
            total_profit_units=round(float(total_profit), 4),
            roi=round(roi, 5) if roi is not None else None,
            yield_pct=round(yield_pct, 3) if yield_pct is not None else None,
            avg_probability=round(mean(probabilities), 5) if probabilities else None,
            avg_edge=round(mean(edges), 5) if edges else None,
            avg_expected_value=round(mean(evs), 5) if evs else None,
        )

    def report(self, *, model_version: str, premium_only: bool = False) -> list[MetricSummary]:
        outcomes = list(
            PredictionOutcome.objects.select_related("prediction", "prediction__fixture")
            .filter(prediction__model_version=model_version)
            .exclude(result=PredictionOutcome.RESULT_PENDING)
        )
        if premium_only:
            outcomes = [row for row in outcomes if row.prediction.tier == "TIER_A"]

        groups: dict[str, list[PredictionOutcome]] = defaultdict(list)
        for row in outcomes:
            prediction = row.prediction
            groups["all"].append(row)
            groups[f"market:{prediction.market}"].append(row)
            groups[f"tier:{prediction.tier or 'NONE'}"].append(row)
            for scope in self._active_rule_scopes(prediction):
                groups[scope].append(row)

        return [self._summary(scope, rows) for scope, rows in sorted(groups.items())]

    def persist_report(self, *, model_version: str, premium_only: bool = False) -> list[LearningSnapshot]:
        summaries = self.report(model_version=model_version, premium_only=premium_only)
        snapshots = [
            LearningSnapshot(
                model_version=model_version,
                scope=summary.scope,
                sample_size=summary.sample_size,
                wins=summary.wins,
                losses=summary.losses,
                voids=summary.voids,
                win_rate=summary.win_rate,
                roi=summary.roi,
                yield_pct=summary.yield_pct,
                avg_probability=summary.avg_probability,
                avg_edge=summary.avg_edge,
                avg_expected_value=summary.avg_expected_value,
                total_profit_units=summary.total_profit_units,
                metadata={"premium_only": premium_only},
            )
            for summary in summaries
        ]
        if snapshots:
            LearningSnapshot.objects.bulk_create(snapshots, batch_size=LEARNING_BATCH_SIZE)
        return snapshots
