from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta
from typing import Any

from django.utils import timezone

from .candidate_pool import CandidatePoolRule, high_recall_candidate_pool
from .competition_quality import classify_competition
from .models import Prediction
from .premium_selection import DailyPremiumSelector
from .score_v8 import V8_MODEL_VERSION


FAILURE_LABELS = {
    "insufficient_data_quality": "Calidad de datos insuficiente",
    "insufficient_home_venue_sample": "Poca muestra local",
    "insufficient_away_venue_sample": "Poca muestra visitante",
    "market_confidence_below_50": "Market Confidence insuficiente",
    "market_intelligence_below_40": "Market Intelligence insuficiente",
    "prefer_btts_over_over25": "BTTS tiene mejor encaje que Over 2.5",
    "extreme_btts_over_mismatch": "BTTS alto pero baja escalación a Over 2.5",
    "extreme_low_score_script": "Guion extremo de pocos goles",
    "extreme_btts_contradiction": "Contradicción extrema para BTTS",
    "away_over25_very_low": "Over 2.5 visitante muy bajo",
    "away_over25_low": "Over 2.5 visitante bajo",
    "away_total_goals_very_low": "Promedio visitante de goles muy bajo",
    "away_total_goals_low": "Promedio visitante de goles bajo",
    "h2h_over25_low": "H2H Over 2.5 bajo",
    "competition_over25_low": "Competición con Over 2.5 bajo",
    "away_failed_to_score_high": "Visitante se queda sin marcar con frecuencia",
    "home_clean_sheet_high": "Local mantiene portería a cero con frecuencia",
    "h2h_btts_low": "H2H BTTS bajo",
    "competition_btts_low": "Competición con BTTS bajo",
}


class ModelDiagnosticsService:
    """Explain where future predictions are lost in the Premium funnel."""

    def __init__(self, model_version: str = V8_MODEL_VERSION):
        self.model_version = model_version

    @staticmethod
    def _bounds(target_date):
        start = timezone.make_aware(datetime.combine(target_date, time.min))
        return start, start + timedelta(days=1)

    @staticmethod
    def _failure_label(code: str) -> str:
        return FAILURE_LABELS.get(code, code.replace("_", " ").title())

    def build(self, target_date=None, *, top_rejected: int = 5) -> dict[str, Any]:
        target_date = target_date or timezone.localdate()
        start, end = self._bounds(target_date)
        future_start = max(start, timezone.now())
        predictions = list(
            Prediction.objects.select_related(
                "fixture",
                "fixture__home_team",
                "fixture__away_team",
                "fixture__competition_ref",
            )
            .filter(
                model_version=self.model_version,
                fixture__kickoff__gte=future_start,
                fixture__kickoff__lt=end,
            )
            .order_by("-score", "-expected_value")
        )

        official = [p for p in predictions if not classify_competition(p.fixture).excluded]
        pool = high_recall_candidate_pool(
            target_date,
            rule=CandidatePoolRule(limit=12),
            model_version=self.model_version,
        )
        pool_prediction_ids = {entry.prediction_id for entry in pool}

        market_confidence_pass = []
        intelligence_pass = []
        base_data_pass = []
        premium_eligible = []
        rejection_counter: Counter[str] = Counter()
        rejected_rows: list[dict[str, Any]] = []

        for prediction in official:
            reasons = prediction.reasons or {}
            failures = list(reasons.get("v8_gate_failures") or [])
            base_failures = [
                item for item in failures
                if item in {
                    "insufficient_data_quality",
                    "insufficient_home_venue_sample",
                    "insufficient_away_venue_sample",
                }
            ]
            if not base_failures:
                base_data_pass.append(prediction)
            if reasons.get("market_confidence_passed") is True:
                market_confidence_pass.append(prediction)
            if reasons.get("market_intelligence_passed") is True:
                intelligence_pass.append(prediction)
            if DailyPremiumSelector._tier_for(prediction) is not None:
                premium_eligible.append(prediction)

            if DailyPremiumSelector._tier_for(prediction) is None:
                if failures:
                    main = failures[0]
                elif prediction.market_odds is None:
                    main = "missing_market_odds"
                elif prediction.edge is None or float(prediction.edge) < 0.05:
                    main = "edge_below_premium_floor"
                elif prediction.expected_value is None or float(prediction.expected_value) < 0.06:
                    main = "ev_below_premium_floor"
                elif float(prediction.score) < 84.0:
                    main = "score_below_premium_floor"
                else:
                    main = "premium_threshold_combination"
                rejection_counter[main] += 1
                rejected_rows.append({
                    "prediction": prediction,
                    "reason_code": main,
                    "reason": self._failure_label(main),
                    "market_confidence": reasons.get("market_confidence_score"),
                    "market_intelligence": reasons.get("market_intelligence_score"),
                    "gei": (reasons.get("market_intelligence_evidence") or {}).get("goal_escalation_index"),
                    "low_score_rate": (reasons.get("market_intelligence_evidence") or {}).get("combined_low_score_rate"),
                    "in_high_recall_pool": prediction.id in pool_prediction_ids,
                })

        rejected_rows.sort(
            key=lambda row: (
                float(row["prediction"].score or 0),
                float(row["prediction"].expected_value or -1),
                float(row["prediction"].edge or -1),
            ),
            reverse=True,
        )

        unique_fixtures = len({p.fixture_id for p in predictions})
        official_fixtures = len({p.fixture_id for p in official})
        base_fixtures = len({p.fixture_id for p in base_data_pass})
        confidence_fixtures = len({p.fixture_id for p in market_confidence_pass})
        intelligence_fixtures = len({p.fixture_id for p in intelligence_pass})
        premium_fixtures = len({p.fixture_id for p in premium_eligible})

        return {
            "target_date": target_date,
            "funnel": [
                {"label": "Fixtures con predicción V8", "count": unique_fixtures},
                {"label": "Competiciones oficiales", "count": official_fixtures},
                {"label": "High Recall Pool", "count": len(pool)},
                {"label": "Calidad/muestra suficiente", "count": base_fixtures},
                {"label": "Market Confidence OK", "count": confidence_fixtures},
                {"label": "Market Intelligence OK", "count": intelligence_fixtures},
                {"label": "Elegibles Premium", "count": premium_fixtures},
            ],
            "rejection_summary": [
                {"code": code, "reason": self._failure_label(code), "count": count}
                for code, count in rejection_counter.most_common(10)
            ],
            "top_rejected": rejected_rows[: max(1, int(top_rejected))],
            "pool_count": len(pool),
            "prediction_count": len(predictions),
        }
