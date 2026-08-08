from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from django.db.models import Prefetch
from django.utils import timezone

from backtesting.models import PredictionOutcome
from backtesting.services import LearningAnalyticsService
from engine.models import Prediction
from engine.score_v8 import V8_MODEL_VERSION


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

    def build(self) -> dict[str, Any]:
        return {
            "model_version": self.model_version,
            "metrics": self.metrics().to_dict(),
            "premium_picks": self.premium_picks(),
            "recent_results": self.recent_results(),
            "market_performance": self.market_performance(),
            "rule_performance": self.rule_performance(),
            "generated_at": timezone.now(),
        }
