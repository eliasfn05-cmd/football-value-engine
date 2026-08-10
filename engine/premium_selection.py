from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .competition_quality import classify_competition
from .deep_analysis import DEEP_ANALYSIS_VERSION
from .models import DailyPremiumSelection, Prediction
from .probability_calibration import (
    PREMIUM_MIN_RELIABILITY,
    TIER_A_MIN_RELIABILITY,
    ProbabilityEVCalibrationService,
)
from .score_v8 import V8_MODEL_VERSION
from .value_policy import PREMIUM_MIN_EV, TIER_A_MIN_EV, is_premium_value_odds


@dataclass(frozen=True)
class TierRule:
    name: str
    min_score: float
    min_edge: float
    min_ev: float
    min_btts_probability: float
    min_over25_probability: float
    min_reliability: float


TIER_RULES = (
    TierRule("A", 90.0, 0.07, float(TIER_A_MIN_EV), 0.58, 0.60, TIER_A_MIN_RELIABILITY),
    TierRule("B", 84.0, 0.05, float(PREMIUM_MIN_EV), 0.54, 0.56, PREMIUM_MIN_RELIABILITY),
)

DYNAMIC_SCORE_FLOORS = (84.0, 82.0, 80.0, 78.0, 76.0)

# Sprint 7.6 - Two-goal ceiling / fragile Over guard.
FRAGILE_OVER_MAX_RAW_PROBABILITY = 0.60
FRAGILE_OVER_MIN_ODDS = 2.00
FRAGILE_OVER_MAX_COMBINED_BTTS = 0.50

# Sprint 7.7 - Market disagreement guard.
# A large model-vs-market probability gap is not an automatic rejection: it can
# be genuine value. It does, however, lower effective reliability and rank so
# an overconfident raw estimate cannot dominate the Premium list unchecked.
DISAGREEMENT_FREE_GAP = 0.12
DISAGREEMENT_MAX_GAP = 0.30
DISAGREEMENT_MAX_RELIABILITY_PENALTY = 0.10
DISAGREEMENT_MAX_RANK_PENALTY = 8.0


class DailyPremiumSelector:
    """Select up to three Deep-validated, market-calibrated Premium Value markets."""

    calibrator = ProbabilityEVCalibrationService()

    def __init__(self, model_version: str = V8_MODEL_VERSION, max_picks: int = 3):
        self.model_version = model_version
        self.max_picks = max(1, min(int(max_picks), 3))

    @staticmethod
    def _bounds(target_date: date):
        start = timezone.make_aware(datetime.combine(target_date, time.min))
        return start, start + timedelta(days=1)

    @staticmethod
    def _probability_floor(rule: TierRule, market: str) -> float:
        return rule.min_btts_probability if market == "BTTS" else rule.min_over25_probability

    @staticmethod
    def _base_probability_floor(market: str) -> float:
        if market == "BTTS":
            return 0.54
        if market == "OVER_2_5":
            return 0.56
        return 1.0

    @classmethod
    def _disagreement_metrics(cls, prediction: Prediction) -> dict:
        calibration = cls.calibrator.calibrate(prediction)
        gap = max(0.0, calibration.raw_probability - calibration.implied_probability)
        span = max(0.001, DISAGREEMENT_MAX_GAP - DISAGREEMENT_FREE_GAP)
        severity = max(0.0, min(1.0, (gap - DISAGREEMENT_FREE_GAP) / span))
        reliability_penalty = severity * DISAGREEMENT_MAX_RELIABILITY_PENALTY
        rank_penalty = severity * DISAGREEMENT_MAX_RANK_PENALTY
        return {
            "gap": gap,
            "severity": severity,
            "reliability_penalty": reliability_penalty,
            "rank_penalty": rank_penalty,
            "effective_reliability": max(0.0, calibration.reliability - reliability_penalty),
        }

    @classmethod
    def _market_eligible_deep_preference(cls, prediction: Prediction) -> bool:
        """Sprint 7.7: Deep preference is evaluated only among bettable markets.

        If this market is the original Deep preferred market, it passes. If it
        is not, it may act as the fallback only when the original preferred
        sibling for the same fixture/model is outside Premium odds (or has no
        usable price). This fixes the deadlock where an unbettable 1.38 Over
        blocked a valid 1.62 BTTS without weakening any value/EV/reliability gate.
        """
        reasons = prediction.reasons or {}
        if reasons.get("deep_preferred_market") is True:
            return True

        siblings = Prediction.objects.filter(
            fixture_id=prediction.fixture_id,
            model_version=prediction.model_version,
            market__in={"BTTS", "OVER_2_5"},
        ).exclude(pk=prediction.pk)

        preferred_siblings = [
            sibling for sibling in siblings
            if (sibling.reasons or {}).get("deep_preferred_market") is True
        ]
        if not preferred_siblings:
            return False

        # A non-preferred market is a fallback only when every Deep-preferred
        # sibling is operationally unavailable under the hard Premium odds rule.
        for sibling in preferred_siblings:
            if sibling.market_odds is not None and is_premium_value_odds(sibling.market_odds):
                return False
        return True

    @classmethod
    def _fragile_over25_profile(cls, prediction: Prediction) -> bool:
        if prediction.market != "OVER_2_5" or prediction.market_odds is None:
            return False
        calibration = cls.calibrator.calibrate(prediction)
        if calibration.raw_probability >= FRAGILE_OVER_MAX_RAW_PROBABILITY:
            return False
        if float(prediction.market_odds) < FRAGILE_OVER_MIN_ODDS:
            return False
        evidence = (prediction.reasons or {}).get("deep_analysis_evidence") or {}
        try:
            home_btts = float(evidence.get("home_btts_rate"))
            away_btts = float(evidence.get("away_btts_rate"))
        except (TypeError, ValueError):
            return False
        return (home_btts + away_btts) / 2.0 <= FRAGILE_OVER_MAX_COMBINED_BTTS

    @classmethod
    def _passes_hard_value_floors(cls, prediction: Prediction) -> bool:
        if classify_competition(prediction.fixture).excluded:
            return False
        reasons = prediction.reasons or {}
        if not reasons.get("v8_gates_passed", False):
            return False
        if reasons.get("deep_analysis_version") != DEEP_ANALYSIS_VERSION:
            return False
        if reasons.get("deep_analysis_passed") is not True:
            return False
        if not cls._market_eligible_deep_preference(prediction):
            return False
        if prediction.market_odds is None or prediction.edge is None or prediction.expected_value is None:
            return False
        if not is_premium_value_odds(prediction.market_odds):
            return False
        if prediction.market not in {"BTTS", "OVER_2_5"}:
            return False

        calibration = cls.calibrator.calibrate(prediction)
        disagreement = cls._disagreement_metrics(prediction)
        if not calibration.premium_reliable:
            return False
        if disagreement["effective_reliability"] < PREMIUM_MIN_RELIABILITY:
            return False
        if calibration.raw_probability < cls._base_probability_floor(prediction.market):
            return False
        if calibration.calibrated_edge < 0.05:
            return False
        if calibration.reliable_ev < float(PREMIUM_MIN_EV):
            return False
        if cls._fragile_over25_profile(prediction):
            return False
        return True

    @classmethod
    def rejection_reasons(cls, prediction: Prediction, *, score_floor: float = 76.0) -> list[str]:
        out: list[str] = []
        quality = classify_competition(prediction.fixture)
        if quality.excluded:
            return [f"competition:{quality.reason}"]
        reasons = prediction.reasons or {}
        if not reasons.get("v8_gates_passed", False):
            out.append("v8_gates")
        if reasons.get("deep_analysis_version") != DEEP_ANALYSIS_VERSION:
            out.append("deep_missing")
        elif reasons.get("deep_analysis_passed") is not True:
            out.append("deep_rejected")
        if not cls._market_eligible_deep_preference(prediction):
            out.append("not_market_eligible_deep_preferred")
        if prediction.market_odds is None:
            out.append("no_odds")
            return out
        if not is_premium_value_odds(prediction.market_odds):
            out.append("odds_outside_1.60_2.40")

        calibration = cls.calibrator.calibrate(prediction)
        disagreement = cls._disagreement_metrics(prediction)
        if calibration.reliability < PREMIUM_MIN_RELIABILITY:
            out.append(f"reliability:{calibration.reliability:.3f}")
        if disagreement["effective_reliability"] < PREMIUM_MIN_RELIABILITY:
            out.append(
                f"disagreement_reliability:{disagreement['effective_reliability']:.3f}"
                f"/gap:{disagreement['gap']:.3f}"
            )
        if calibration.raw_probability < cls._base_probability_floor(prediction.market):
            out.append(f"raw_probability:{calibration.raw_probability:.3f}")
        if calibration.calibrated_edge < 0.05:
            out.append(f"calibrated_edge:{calibration.calibrated_edge:.3f}")
        if calibration.reliable_ev < float(PREMIUM_MIN_EV):
            out.append(f"reliable_ev:{calibration.reliable_ev:.3f}")
        if cls._fragile_over25_profile(prediction):
            out.append("fragile_over25_two_goal_ceiling")
        if float(prediction.score or 0.0) < score_floor:
            out.append(f"score:{float(prediction.score or 0.0):.1f}")
        return out

    @classmethod
    def _tier_for(cls, prediction: Prediction, *, score_floor: float = 84.0) -> str | None:
        if not cls._passes_hard_value_floors(prediction):
            return None
        calibration = cls.calibrator.calibrate(prediction)
        disagreement = cls._disagreement_metrics(prediction)
        probability = calibration.raw_probability
        score = float(prediction.score)
        edge = calibration.calibrated_edge
        ev = calibration.reliable_ev
        reliability = disagreement["effective_reliability"]

        for rule in TIER_RULES:
            effective_score = rule.min_score
            if rule.name == "B":
                effective_score = min(effective_score, score_floor)
            if (
                score >= effective_score
                and edge >= rule.min_edge
                and ev >= rule.min_ev
                and probability >= cls._probability_floor(rule, prediction.market)
                and reliability >= rule.min_reliability
            ):
                return rule.name
        return None

    @classmethod
    def _rank_score(cls, prediction: Prediction) -> tuple[float, dict]:
        calibration = cls.calibrator.calibrate(prediction)
        disagreement = cls._disagreement_metrics(prediction)
        score_component = max(0.0, min(float(prediction.score), 100.0))
        probability_component = calibration.raw_probability * 100.0
        ev_component = min(max(0.0, calibration.reliable_ev) / 0.20, 1.0) * 100.0
        edge_component = min(max(0.0, calibration.calibrated_edge) / 0.12, 1.0) * 100.0
        reliability_component = disagreement["effective_reliability"] * 100.0

        base_composite = (
            0.22 * score_component
            + 0.30 * ev_component
            + 0.20 * edge_component
            + 0.13 * probability_component
            + 0.15 * reliability_component
        )
        composite = max(0.0, base_composite - disagreement["rank_penalty"])
        reasons = prediction.reasons or {}
        rationale = {
            "score_component": round(score_component, 2),
            "probability_component": round(probability_component, 2),
            "ev_component": round(ev_component, 2),
            "edge_component": round(edge_component, 2),
            "reliability_component": round(reliability_component, 2),
            "deep_analysis_version": reasons.get("deep_analysis_version"),
            "deep_analysis_evidence": reasons.get("deep_analysis_evidence") or {},
            "deep_analysis_warnings": reasons.get("deep_analysis_warnings") or [],
            "deep_score": reasons.get("deep_score"),
            "market_eligible_deep_preference": cls._market_eligible_deep_preference(prediction),
            "original_deep_preferred_market": reasons.get("deep_preferred_market") is True,
            "probability_calibration": calibration.as_dict(),
            "raw_probability": calibration.raw_probability,
            "calibrated_probability": calibration.calibrated_probability,
            "market_implied_probability": calibration.implied_probability,
            "market_odds": float(prediction.market_odds) if prediction.market_odds is not None else None,
            "raw_edge": calibration.raw_edge,
            "calibrated_edge": calibration.calibrated_edge,
            "raw_expected_value": calibration.raw_ev,
            "calibrated_expected_value": calibration.calibrated_ev,
            "reliable_expected_value": calibration.reliable_ev,
            "probability_reliability": calibration.reliability,
            "effective_reliability_after_disagreement": round(disagreement["effective_reliability"], 4),
            "model_market_probability_gap": round(disagreement["gap"], 4),
            "disagreement_severity": round(disagreement["severity"], 4),
            "disagreement_reliability_penalty": round(disagreement["reliability_penalty"], 4),
            "disagreement_rank_penalty": round(disagreement["rank_penalty"], 2),
            "rank_before_disagreement_penalty": round(base_composite, 2),
            "odds_policy": "Premium Value 1.60-2.40",
            "value_gate": "Sprint 7.7 market-eligible Deep + raw probability + calibrated edge/EV + disagreement + fragile-Over guard",
            "fragile_over25_guard": cls._fragile_over25_profile(prediction),
            "formula": "0.22*deep_score + 0.30*reliable_ev + 0.20*calibrated_edge + 0.13*raw_probability + 0.15*effective_reliability - disagreement_penalty",
        }
        return round(composite, 2), rationale

    def _rank_candidates(self, candidates: list[Prediction], score_floor: float):
        ranked = []
        for prediction in candidates:
            if classify_competition(prediction.fixture).excluded:
                continue
            tier = self._tier_for(prediction, score_floor=score_floor)
            if tier is None:
                continue
            rank_score, rationale = self._rank_score(prediction)
            rationale["premium_tier"] = tier
            rationale["dynamic_score_floor"] = score_floor
            ranked.append((prediction, tier, rank_score, rationale))

        tier_priority = {"A": 2, "B": 1}
        ranked.sort(
            key=lambda item: (
                tier_priority[item[1]],
                item[2],
                float(item[3].get("reliable_expected_value") or 0),
                float(item[0].score),
            ),
            reverse=True,
        )
        return ranked

    @staticmethod
    def _unique_fixture_count(ranked) -> int:
        return len({item[0].fixture_id for item in ranked})

    @transaction.atomic
    def select(self, target_date: date) -> list[DailyPremiumSelection]:
        start, end = self._bounds(target_date)
        future_start = max(start, timezone.now())
        candidates = list(
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
                market_odds__gte=Decimal("1.60"),
                market_odds__lte=Decimal("2.40"),
                edge__isnull=False,
                expected_value__gte=PREMIUM_MIN_EV,
            )
        )

        ranked = []
        selected_floor = DYNAMIC_SCORE_FLOORS[-1]
        for score_floor in DYNAMIC_SCORE_FLOORS:
            current = self._rank_candidates(candidates, score_floor)
            ranked = current
            selected_floor = score_floor
            if self._unique_fixture_count(current) >= self.max_picks:
                break

        chosen = []
        seen_fixtures = set()
        for item in ranked:
            prediction = item[0]
            if prediction.fixture_id in seen_fixtures:
                continue
            chosen.append(item)
            seen_fixtures.add(prediction.fixture_id)
            if len(chosen) >= self.max_picks:
                break

        DailyPremiumSelection.objects.filter(
            target_date=target_date,
            model_version=self.model_version,
        ).delete()

        rows = []
        for index, (prediction, tier, rank_score, rationale) in enumerate(chosen, start=1):
            rationale["selector_dynamic_floor_used"] = selected_floor
            rows.append(
                DailyPremiumSelection(
                    target_date=target_date,
                    prediction=prediction,
                    rank=index,
                    premium_tier=tier,
                    premium_rank_score=Decimal(f"{rank_score:.2f}"),
                    model_version=self.model_version,
                    rationale=rationale,
                )
            )
        if rows:
            DailyPremiumSelection.objects.bulk_create(rows)
        return list(
            DailyPremiumSelection.objects.select_related(
                "prediction",
                "prediction__fixture",
                "prediction__fixture__home_team",
                "prediction__fixture__away_team",
                "prediction__fixture__competition_ref",
            )
            .filter(target_date=target_date, model_version=self.model_version)
            .order_by("rank")
        )
