from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta
from typing import Any

from django.utils import timezone

from .candidate_pool import CandidatePoolRule, high_recall_candidate_pool
from .competition_quality import classify_competition
from .deep_analysis import DEEP_ANALYSIS_VERSION
from .models import Prediction
from .premium_selection import DailyPremiumSelector
from .score_v8 import V8_MODEL_VERSION


FAILURE_LABELS = {
    "no_home_venue_history": "Sin historial local por condición",
    "no_away_venue_history": "Sin historial visitante por condición",
    "data_quality_soft_penalty": "Calidad de datos penalizada",
    "home_venue_sample_soft_penalty": "Muestra local pequeña penalizada",
    "away_venue_sample_soft_penalty": "Muestra visitante pequeña penalizada",
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
    "missing_market_odds": "Sin cuota de mercado",
    "edge_below_premium_floor": "Edge por debajo de 5%",
    "ev_below_premium_floor": "EV por debajo de 6%",
    "probability_below_premium_floor": "Probabilidad por debajo del piso Premium",
    "score_below_dynamic_floor": "Score final por debajo del piso dinámico 80",
    "deep_analysis_pending": "Deep Analysis pendiente/no disponible",
    "deep_market_not_preferred": "Mercado descartado: otro mercado del partido tiene mejor Deep Score",
    "deep_analysis_rejected": "Deep Analysis rechazó el mercado",
    "deep_over25_pattern_rejected": "Deep: patrón Over 2.5 insuficiente",
    "deep_over25_low_score_rejected": "Deep: perfil de pocos goles incompatible con Over 2.5",
    "deep_btts_pattern_rejected": "Deep: patrón BTTS insuficiente",
    "deep_btts_scoring_rejected": "Deep: riesgo de quedarse sin marcar incompatible con BTTS",
    "premium_threshold_combination": "No supera la combinación de umbrales Premium",
}


class ModelDiagnosticsService:
    """Explain the real sequential Premium funnel, including Sprint 7.0."""

    def __init__(self, model_version: str = V8_MODEL_VERSION):
        self.model_version = model_version

    @staticmethod
    def _bounds(target_date):
        start = timezone.make_aware(datetime.combine(target_date, time.min))
        return start, start + timedelta(days=1)

    @staticmethod
    def _failure_label(code: str) -> str:
        return FAILURE_LABELS.get(code, code.replace("_", " ").title())

    @staticmethod
    def _probability_ok(prediction: Prediction) -> bool:
        p = float(prediction.probability)
        if prediction.market == "BTTS":
            return p >= 0.59
        if prediction.market == "OVER_2_5":
            return p >= 0.61
        return False

    @staticmethod
    def _deep_state(prediction: Prediction) -> dict[str, Any]:
        reasons = prediction.reasons or {}
        state = reasons.get("deep_analysis")
        if isinstance(state, dict) and state.get("version") == DEEP_ANALYSIS_VERSION:
            return state
        if reasons.get("deep_analysis_version") != DEEP_ANALYSIS_VERSION:
            return {"status": "pending"}
        return {
            "status": reasons.get("deep_analysis_status") or "complete",
            "passed": reasons.get("deep_analysis_passed"),
            "preferred_market": reasons.get("deep_preferred_market"),
            "score": reasons.get("deep_score"),
            "warnings": reasons.get("deep_analysis_warnings") or [],
            "failures": reasons.get("deep_analysis_failures") or [],
        }

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
        official_ids = {p.id for p in official}
        discovery_pool = high_recall_candidate_pool(
            target_date,
            rule=CandidatePoolRule(limit=60),
            model_version=self.model_version,
        )
        pool_prediction_ids = {entry.prediction_id for entry in discovery_pool}
        pool_predictions = [p for p in official if p.id in pool_prediction_ids]

        hard_data_pass = []
        confidence_pass = []
        intelligence_pass = []
        deep_complete = []
        deep_preferred = []
        value_pass = []
        premium_eligible = []
        rejection_counter: Counter[str] = Counter()
        rejected_rows: list[dict[str, Any]] = []

        for prediction in pool_predictions:
            reasons = prediction.reasons or {}
            failures = list(reasons.get("v8_gate_failures") or [])
            hard_data_failures = [item for item in failures if item in {"no_home_venue_history", "no_away_venue_history"}]
            if hard_data_failures:
                continue
            hard_data_pass.append(prediction)

            if reasons.get("market_confidence_passed") is not True:
                continue
            confidence_pass.append(prediction)

            if reasons.get("market_intelligence_passed") is not True:
                continue
            intelligence_pass.append(prediction)

            deep = self._deep_state(prediction)
            if deep.get("status") != "complete":
                continue
            deep_complete.append(prediction)
            if deep.get("passed") is not True or deep.get("preferred_market") is not True:
                continue
            deep_preferred.append(prediction)

            if not DailyPremiumSelector._passes_hard_value_floors(prediction):
                continue
            value_pass.append(prediction)

            if DailyPremiumSelector._tier_for(prediction, score_floor=80.0) is not None:
                premium_eligible.append(prediction)

        for prediction in official:
            if DailyPremiumSelector._tier_for(prediction, score_floor=80.0) is not None:
                continue
            reasons = prediction.reasons or {}
            failures = list(reasons.get("v8_gate_failures") or [])
            soft_warnings = list(reasons.get("v8_soft_warnings") or [])
            deep = self._deep_state(prediction)
            if failures:
                main = failures[0]
            elif deep.get("status") != "complete":
                main = "deep_analysis_pending"
            elif deep.get("passed") is False:
                deep_failures = list(deep.get("failures") or [])
                main = deep_failures[0] if deep_failures else "deep_analysis_rejected"
            elif deep.get("preferred_market") is False:
                main = "deep_market_not_preferred"
            elif prediction.market_odds is None:
                main = "missing_market_odds"
            elif not self._probability_ok(prediction):
                main = "probability_below_premium_floor"
            elif prediction.edge is None or float(prediction.edge) < 0.05:
                main = "edge_below_premium_floor"
            elif prediction.expected_value is None or float(prediction.expected_value) < 0.06:
                main = "ev_below_premium_floor"
            elif float(prediction.score) < 80.0:
                main = "score_below_dynamic_floor"
            else:
                main = "premium_threshold_combination"
            rejection_counter[main] += 1
            for warning in soft_warnings:
                rejection_counter[warning] += 1
            rejected_rows.append({
                "prediction": prediction,
                "reason_code": main,
                "reason": self._failure_label(main),
                "soft_warnings": [self._failure_label(item) for item in soft_warnings],
                "market_confidence": reasons.get("market_confidence_score"),
                "market_intelligence": reasons.get("market_intelligence_score"),
                "gei": (reasons.get("market_intelligence_evidence") or {}).get("goal_escalation_index"),
                "low_score_rate": (reasons.get("market_intelligence_evidence") or {}).get("combined_low_score_rate"),
                "sample_confidence": reasons.get("venue_sample_confidence"),
                "evidence_penalty": reasons.get("evidence_penalty"),
                "deep_status": deep.get("status"),
                "deep_score": deep.get("score"),
                "deep_preferred": deep.get("preferred_market"),
                "in_high_recall_pool": prediction.id in pool_prediction_ids,
            })

        rejected_rows.sort(
            key=lambda row: (
                row["in_high_recall_pool"],
                float(row["prediction"].score or 0),
                float(row["prediction"].expected_value or -1),
                float(row["prediction"].edge or -1),
            ),
            reverse=True,
        )

        return {
            "target_date": target_date,
            "funnel": [
                {"label": "Fixtures con predicción V8", "count": len({p.fixture_id for p in predictions})},
                {"label": "Competiciones oficiales", "count": len({p.fixture_id for p in official})},
                {"label": "High Recall Discovery Pool", "count": len({p.fixture_id for p in pool_predictions})},
                {"label": "Historial mínimo disponible", "count": len({p.fixture_id for p in hard_data_pass})},
                {"label": "Market Confidence OK", "count": len({p.fixture_id for p in confidence_pass})},
                {"label": "Market Intelligence OK", "count": len({p.fixture_id for p in intelligence_pass})},
                {"label": "Deep Analysis completo", "count": len({p.fixture_id for p in deep_complete})},
                {"label": "Mercado Deep preferido", "count": len({p.fixture_id for p in deep_preferred})},
                {"label": "Valor mínimo EV/Edge/Prob.", "count": len({p.fixture_id for p in value_pass})},
                {"label": "Elegibles Premium (piso dinámico)", "count": len({p.fixture_id for p in premium_eligible})},
            ],
            "rejection_summary": [
                {"code": code, "reason": self._failure_label(code), "count": count}
                for code, count in rejection_counter.most_common(12)
            ],
            "top_rejected": rejected_rows[: max(1, int(top_rejected))],
            "pool_count": len(discovery_pool),
            "prediction_count": len(predictions),
            "official_prediction_count": len(official_ids),
        }
