from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

from django.utils import timezone

from backtesting.models import PredictionOutcome
from backtesting.services import LearningAnalyticsService
from engine.models import Prediction
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
    """Prepare read-only, presentation-friendly data for the web dashboard."""

    REJECTION_LABELS = {
        "insufficient_data_quality": "Calidad insuficiente",
        "insufficient_home_venue_sample": "Poca muestra local",
        "insufficient_away_venue_sample": "Poca muestra visitante",
        "missing_odds": "Sin cuota",
        "probability": "Probabilidad baja",
        "edge": "Edge < 6%",
        "ev": "EV < 8%",
        "score": "Score < 80",
    }

    def __init__(self, model_version: str = V8_MODEL_VERSION):
        self.model_version = model_version

    def metrics(self, *, premium_only: bool = True) -> DashboardMetrics:
        summaries = LearningAnalyticsService().report(
            model_version=self.model_version,
            premium_only=premium_only,
        )
        overall = next((item for item in summaries if item.scope == "all"), None)
        if overall is None:
            return DashboardMetrics()
        return DashboardMetrics(
            sample_size=overall.sample_size,
            wins=overall.wins,
            losses=overall.losses,
            voids=overall.voids,
            win_rate_pct=round(overall.win_rate * 100, 1) if overall.win_rate is not None else None,
            profit_units=overall.total_profit_units,
            roi_pct=round(overall.roi * 100, 1) if overall.roi is not None else None,
            yield_pct=overall.yield_pct,
        )

    def premium_picks(self, *, limit: int = 10) -> list[Prediction]:
        return list(
            Prediction.objects.select_related("fixture", "fixture__home_team", "fixture__away_team")
            .filter(
                model_version=self.model_version,
                tier="TIER_A",
                fixture__kickoff__gte=timezone.now(),
            )
            .order_by("fixture__kickoff", "-score")[:limit]
        )

    @classmethod
    def _rejection_codes(cls, prediction: Prediction) -> list[str]:
        codes: list[str] = []
        reasons = prediction.reasons or {}
        for failure in reasons.get("v8_gate_failures") or []:
            if failure not in codes:
                codes.append(failure)
        if prediction.market_odds is None:
            codes.append("missing_odds")
        probability = float(prediction.probability)
        probability_floor = 0.63 if prediction.market == "BTTS" else 0.65
        if probability < probability_floor:
            codes.append("probability")
        if prediction.edge is None or float(prediction.edge) < 0.06:
            codes.append("edge")
        if prediction.expected_value is None or float(prediction.expected_value) < 0.08:
            codes.append("ev")
        if float(prediction.score) < 80.0:
            codes.append("score")
        return codes

    @classmethod
    def _rejection_reason(cls, prediction: Prediction) -> str:
        labels = {
            **cls.REJECTION_LABELS,
            "insufficient_data_quality": "Calidad de datos insuficiente",
            "insufficient_home_venue_sample": "Poca muestra local",
            "insufficient_away_venue_sample": "Poca muestra visitante",
            "missing_odds": "Sin cuota de mercado",
            "probability": "Probabilidad BTTS < 63%" if prediction.market == "BTTS" else "Probabilidad Over 2.5 < 65%",
        }
        codes = cls._rejection_codes(prediction)
        return ", ".join(labels.get(code, code) for code in codes) if codes else "No cumple todos los filtros Premium"

    def _future_non_premium_qs(self):
        return (
            Prediction.objects.select_related("fixture", "fixture__home_team", "fixture__away_team")
            .filter(
                model_version=self.model_version,
                fixture__kickoff__gte=timezone.now(),
            )
            .exclude(tier="TIER_A")
        )

    def near_premium(self, *, limit: int = 8) -> list[dict[str, Any]]:
        qs = self._future_non_premium_qs().order_by("-score", "-expected_value", "fixture__kickoff")[:limit]
        return [
            {
                "prediction": pred,
                "reason": self._rejection_reason(pred),
            }
            for pred in qs
        ]

    def rejection_summary(self) -> list[dict[str, Any]]:
        counts: Counter[str] = Counter()
        total = 0
        for prediction in self._future_non_premium_qs().iterator(chunk_size=1000):
            total += 1
            counts.update(set(self._rejection_codes(prediction)))
        rows = [
            {"code": code, "label": self.REJECTION_LABELS.get(code, code), "count": count}
            for code, count in counts.most_common()
        ]
        if total:
            for row in rows:
                row["pct"] = round(row["count"] / total * 100, 1)
        return rows

    def recent_results(self, *, limit: int = 12) -> list[PredictionOutcome]:
        return list(
            PredictionOutcome.objects.select_related(
                "prediction",
                "prediction__fixture",
                "prediction__fixture__home_team",
                "prediction__fixture__away_team",
            )
            .filter(prediction__model_version=self.model_version, prediction__tier="TIER_A")
            .exclude(result=PredictionOutcome.RESULT_PENDING)
            .order_by("-settled_at")[:limit]
        )

    def market_performance(self) -> list[dict[str, Any]]:
        summaries = LearningAnalyticsService().report(
            model_version=self.model_version,
            premium_only=True,
        )
        rows: list[dict[str, Any]] = []
        for item in summaries:
            if not item.scope.startswith("market:"):
                continue
            rows.append(
                {
                    "market": item.scope.split(":", 1)[1],
                    "sample_size": item.sample_size,
                    "win_rate_pct": round(item.win_rate * 100, 1) if item.win_rate is not None else None,
                    "roi_pct": round(item.roi * 100, 1) if item.roi is not None else None,
                    "profit_units": item.total_profit_units,
                }
            )
        return sorted(rows, key=lambda row: row["sample_size"], reverse=True)

    def rule_performance(self, *, limit: int = 8) -> list[dict[str, Any]]:
        summaries = LearningAnalyticsService().report(
            model_version=self.model_version,
            premium_only=True,
        )
        rows: list[dict[str, Any]] = []
        for item in summaries:
            if not item.scope.startswith("rule:"):
                continue
            rows.append(
                {
                    "rule": item.scope.split(":", 1)[1].replace("_", " ").title(),
                    "scope": item.scope,
                    "sample_size": item.sample_size,
                    "win_rate_pct": round(item.win_rate * 100, 1) if item.win_rate is not None else None,
                    "roi_pct": round(item.roi * 100, 1) if item.roi is not None else None,
                    "profit_units": item.total_profit_units,
                }
            )
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
        return {
            "model_version": self.model_version,
            "metrics": self.metrics().to_dict(),
            "premium_picks": self.premium_picks(),
            "near_premium": self.near_premium(),
            "rejection_summary": self.rejection_summary(),
            "recent_results": self.recent_results(),
            "market_performance": self.market_performance(),
            "rule_performance": self.rule_performance(),
            "pipeline": self.pipeline_status(),
            "generated_at": timezone.now(),
        }
