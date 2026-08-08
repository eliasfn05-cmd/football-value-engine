from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from django.db.models import Avg
from django.utils import timezone

from backtesting.models import PredictionOutcome
from backtesting.services import LearningAnalyticsService
from engine.models import DailyPremiumSelection, Prediction
from engine.score_v8 import V8_MODEL_VERSION
from scanner.models import PipelineRun


@dataclass(frozen=True)
class DashboardMetrics:
    sample_size: int = 0
    wins: int = 0
    losses: int = 0
    voids: int = 0
    win_rate_pct: float | None = None
    profit_units: float = 0.0
    roi_pct: float | None = None
    yield_pct: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DashboardService:
    """Prepare operational and developer views without mixing their concerns."""

    def __init__(self, model_version: str = V8_MODEL_VERSION):
        self.model_version = model_version

    def _operational_outcomes(self):
        return (
            PredictionOutcome.objects.select_related("prediction")
            .filter(
                prediction__model_version=self.model_version,
                prediction__daily_selections__model_version=self.model_version,
            )
            .exclude(result=PredictionOutcome.RESULT_PENDING)
            .distinct()
        )

    def metrics(self, *, premium_only: bool = True) -> DashboardMetrics:
        outcomes = list(self._operational_outcomes())
        if not outcomes:
            return DashboardMetrics()
        wins = sum(1 for row in outcomes if row.result == PredictionOutcome.RESULT_WIN)
        losses = sum(1 for row in outcomes if row.result == PredictionOutcome.RESULT_LOSS)
        voids = sum(1 for row in outcomes if row.result == PredictionOutcome.RESULT_VOID)
        decided = wins + losses
        total_stake = sum((row.stake_units for row in outcomes), Decimal("0"))
        total_profit = sum((row.profit_units for row in outcomes), Decimal("0"))
        win_rate = (wins / decided * 100.0) if decided else None
        roi = (float(total_profit / total_stake) * 100.0) if total_stake else None
        return DashboardMetrics(
            sample_size=len(outcomes),
            wins=wins,
            losses=losses,
            voids=voids,
            win_rate_pct=round(win_rate, 1) if win_rate is not None else None,
            profit_units=round(float(total_profit), 4),
            roi_pct=round(roi, 1) if roi is not None else None,
            yield_pct=round(roi, 1) if roi is not None else None,
        )

    def premium_picks(self, *, limit: int = 3) -> list[DailyPremiumSelection]:
        return list(
            DailyPremiumSelection.objects.select_related(
                "prediction",
                "prediction__fixture",
                "prediction__fixture__home_team",
                "prediction__fixture__away_team",
            )
            .filter(
                model_version=self.model_version,
                prediction__fixture__kickoff__gte=timezone.now(),
            )
            .order_by("target_date", "rank")[:limit]
        )

    def operational_summary(self, premium_picks: list[DailyPremiumSelection]) -> dict[str, Any]:
        future = Prediction.objects.filter(
            model_version=self.model_version,
            fixture__kickoff__gte=timezone.now(),
        )
        unique_fixtures = future.values("fixture_id").distinct().count()
        if premium_picks:
            avg_ev = sum(float(row.prediction.expected_value or 0) for row in premium_picks) / len(premium_picks)
            avg_score = sum(float(row.prediction.score) for row in premium_picks) / len(premium_picks)
            avg_rank = sum(float(row.premium_rank_score) for row in premium_picks) / len(premium_picks)
        else:
            avg_ev = avg_score = avg_rank = None
        return {
            "fixtures_analyzed": unique_fixtures,
            "premium_count": len(premium_picks),
            "avg_ev_pct": round(avg_ev * 100, 1) if avg_ev is not None else None,
            "avg_score": round(avg_score, 1) if avg_score is not None else None,
            "avg_rank_score": round(avg_rank, 1) if avg_rank is not None else None,
            "action": "BET" if premium_picks else "NO_BET",
        }

    @staticmethod
    def _rejection_reason(prediction: Prediction) -> str:
        reasons = prediction.reasons or {}
        failures = reasons.get("v8_gate_failures") or []
        if failures:
            labels = {
                "insufficient_data_quality": "Calidad de datos insuficiente",
                "insufficient_home_venue_sample": "Poca muestra local",
                "insufficient_away_venue_sample": "Poca muestra visitante",
            }
            return ", ".join(labels.get(item, item) for item in failures)
        if prediction.market_odds is None:
            return "Sin cuota de mercado"
        if prediction.market == "BTTS" and float(prediction.probability) < 0.59:
            return "Probabilidad BTTS < 59% (piso Sprint 6)"
        if prediction.market == "OVER_2_5" and float(prediction.probability) < 0.61:
            return "Probabilidad Over 2.5 < 61% (piso Sprint 6)"
        if prediction.edge is None or float(prediction.edge) < 0.05:
            return "Edge < 5% (piso Sprint 6)"
        if prediction.expected_value is None or float(prediction.expected_value) < 0.06:
            return "EV < 6% (piso Sprint 6)"
        if float(prediction.score) < 84.0:
            return "Score < 84 (piso Sprint 6)"
        return "Fuera del Top 3 diario"

    def near_premium(self, *, limit: int = 20) -> list[dict[str, Any]]:
        qs = (
            Prediction.objects.select_related("fixture", "fixture__home_team", "fixture__away_team")
            .filter(model_version=self.model_version, fixture__kickoff__gte=timezone.now())
            .order_by("-score", "-expected_value", "fixture__kickoff")[:limit]
        )
        return [{"prediction": pred, "reason": self._rejection_reason(pred)} for pred in qs]

    def rejection_summary(self) -> list[dict[str, Any]]:
        rows = self.near_premium(limit=200)
        counts: dict[str, int] = {}
        for row in rows:
            reason = row["reason"]
            for part in [item.strip() for item in reason.split(",") if item.strip()]:
                counts[part] = counts.get(part, 0) + 1
        return [
            {"reason": reason, "count": count}
            for reason, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ]

    def recent_results(self, *, limit: int = 12) -> list[PredictionOutcome]:
        return list(
            self._operational_outcomes()
            .select_related(
                "prediction__fixture",
                "prediction__fixture__home_team",
                "prediction__fixture__away_team",
            )
            .order_by("-settled_at")[:limit]
        )

    def market_performance(self) -> list[dict[str, Any]]:
        outcomes = list(self._operational_outcomes())
        grouped: dict[str, list[PredictionOutcome]] = {}
        for row in outcomes:
            grouped.setdefault(row.prediction.market, []).append(row)
        result = []
        for market, rows in grouped.items():
            wins = sum(1 for row in rows if row.result == PredictionOutcome.RESULT_WIN)
            losses = sum(1 for row in rows if row.result == PredictionOutcome.RESULT_LOSS)
            decided = wins + losses
            stake = sum((row.stake_units for row in rows), Decimal("0"))
            profit = sum((row.profit_units for row in rows), Decimal("0"))
            result.append({
                "market": market,
                "sample_size": len(rows),
                "win_rate_pct": round(wins / decided * 100, 1) if decided else None,
                "roi_pct": round(float(profit / stake) * 100, 1) if stake else None,
                "profit_units": float(profit),
            })
        return sorted(result, key=lambda row: row["sample_size"], reverse=True)

    def rule_performance(self, *, limit: int = 8) -> list[dict[str, Any]]:
        summaries = LearningAnalyticsService().report(model_version=self.model_version, premium_only=True)
        rows: list[dict[str, Any]] = []
        for item in summaries:
            if not item.scope.startswith("rule:"):
                continue
            rows.append({
                "rule": item.scope.split(":", 1)[1].replace("_", " ").title(),
                "scope": item.scope,
                "sample_size": item.sample_size,
                "win_rate_pct": round(item.win_rate * 100, 1) if item.win_rate is not None else None,
                "roi_pct": round(item.roi * 100, 1) if item.roi is not None else None,
                "profit_units": item.total_profit_units,
            })
        rows.sort(key=lambda row: (row["sample_size"], row["profit_units"]), reverse=True)
        return rows[:limit]

    def pipeline_status(self) -> dict[str, Any] | None:
        run = PipelineRun.objects.prefetch_related("stages").first()
        if run is None:
            return None
        return {
            "id": run.id,
            "target_date": run.target_date,
            "status": run.status,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "duration_seconds": run.duration_seconds,
            "fixtures_count": run.fixtures_count,
            "predictions_count": run.predictions_count,
            "premium_count": run.premium_count,
            "settled_count": run.settled_count,
            "warning_count": run.warning_count,
            "error_count": run.error_count,
            "stages": list(run.stages.all()),
        }

    def build(self) -> dict[str, Any]:
        picks = self.premium_picks()
        return {
            "model_version": self.model_version,
            "metrics": self.metrics().to_dict(),
            "premium_picks": picks,
            "operational": self.operational_summary(picks),
            "recent_results": self.recent_results(),
            "market_performance": self.market_performance(),
            "rule_performance": self.rule_performance(),
            "pipeline": self.pipeline_status(),
            "generated_at": timezone.now(),
        }

    def build_developer(self) -> dict[str, Any]:
        return {
            "model_version": self.model_version,
            "near_premium": self.near_premium(),
            "rejection_summary": self.rejection_summary(),
            "pipeline": self.pipeline_status(),
            "generated_at": timezone.now(),
        }
