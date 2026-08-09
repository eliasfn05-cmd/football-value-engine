from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any


PROBABILITY_CALIBRATION_VERSION = "sprint7.3"
PREMIUM_MIN_RELIABILITY = 0.55
TIER_A_MIN_RELIABILITY = 0.70


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _float(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if isfinite(result) else default


@dataclass(frozen=True)
class ProbabilityCalibration:
    version: str
    raw_probability: float
    implied_probability: float
    calibrated_probability: float
    raw_edge: float
    calibrated_edge: float
    raw_ev: float
    calibrated_ev: float
    reliable_ev: float
    reliability: float
    reliability_score: float
    support_confidence: float
    coverage_confidence: float
    data_quality_confidence: float
    penalty_confidence: float
    probability_shrinkage: float
    capped_probability_advantage: float

    @property
    def fair_odds(self) -> float:
        return 1.0 / max(self.calibrated_probability, 0.01)

    @property
    def premium_reliable(self) -> bool:
        return self.reliability >= PREMIUM_MIN_RELIABILITY

    @property
    def tier_a_reliable(self) -> bool:
        return self.reliability >= TIER_A_MIN_RELIABILITY

    def as_dict(self) -> dict[str, float | str | bool]:
        return {
            "version": self.version,
            "raw_probability": round(self.raw_probability, 4),
            "implied_probability": round(self.implied_probability, 4),
            "calibrated_probability": round(self.calibrated_probability, 4),
            "raw_edge": round(self.raw_edge, 4),
            "calibrated_edge": round(self.calibrated_edge, 4),
            "raw_ev": round(self.raw_ev, 4),
            "calibrated_ev": round(self.calibrated_ev, 4),
            "reliable_ev": round(self.reliable_ev, 4),
            "reliability": round(self.reliability, 4),
            "reliability_score": round(self.reliability_score, 1),
            "support_confidence": round(self.support_confidence, 4),
            "coverage_confidence": round(self.coverage_confidence, 4),
            "data_quality_confidence": round(self.data_quality_confidence, 4),
            "penalty_confidence": round(self.penalty_confidence, 4),
            "probability_shrinkage": round(self.probability_shrinkage, 4),
            "capped_probability_advantage": round(self.capped_probability_advantage, 4),
            "premium_reliable": self.premium_reliable,
            "tier_a_reliable": self.tier_a_reliable,
        }


class ProbabilityEVCalibrationService:
    """Shrink optimistic model probability toward market probability when evidence is weak.

    The bookmaker price is not treated as truth. It is an anchor that becomes more
    influential only when deep support, sample coverage or data quality are weak.
    This prevents an inflated model probability from creating an equally inflated EV.
    """

    @staticmethod
    def _reliability(reasons: dict[str, Any]) -> tuple[float, float, float, float, float]:
        evidence = reasons.get("deep_analysis_evidence") or {}

        support = _float(evidence.get("market_support_index"), 0.50)
        support_conf = _clamp((support - 0.45) / 0.35)

        coverage = _clamp(_float(evidence.get("sample_coverage"), 0.50))

        data_quality = _clamp(_float(reasons.get("data_quality_score"), 65.0) / 100.0)
        venue_conf = _clamp(_float(reasons.get("venue_sample_confidence"), coverage))
        quality_conf = 0.65 * data_quality + 0.35 * venue_conf

        total_penalty = max(0.0, _float(evidence.get("total_deep_penalty"), 8.0))
        penalty_conf = _clamp(1.0 - total_penalty / 24.0)

        reliability = (
            0.35 * support_conf
            + 0.25 * coverage
            + 0.20 * quality_conf
            + 0.20 * penalty_conf
        )
        reliability = _clamp(reliability, 0.35, 0.95)
        return reliability, support_conf, coverage, quality_conf, penalty_conf

    def calibrate(self, prediction: Any) -> ProbabilityCalibration:
        odds = _float(getattr(prediction, "market_odds", None), 0.0)
        raw_probability = _clamp(_float(getattr(prediction, "probability", None), 0.50), 0.01, 0.99)
        if odds <= 1.0:
            implied = raw_probability
            raw_edge = 0.0
            raw_ev = 0.0
        else:
            implied = _clamp(1.0 / odds, 0.01, 0.99)
            raw_edge = raw_probability - implied
            raw_ev = raw_probability * odds - 1.0

        reasons = getattr(prediction, "reasons", None) or {}
        reliability, support_conf, coverage, quality_conf, penalty_conf = self._reliability(reasons)

        if raw_probability <= implied:
            calibrated_probability = raw_probability
            capped_advantage = max(0.0, raw_probability - implied)
        else:
            # Positive model advantage must earn its distance from the market.
            shrunk = implied + reliability * (raw_probability - implied)
            max_advantage = 0.03 + 0.13 * reliability
            calibrated_probability = min(shrunk, implied + max_advantage)
            capped_advantage = max(0.0, calibrated_probability - implied)

        calibrated_probability = _clamp(calibrated_probability, 0.01, 0.99)
        calibrated_edge = calibrated_probability - implied
        calibrated_ev = calibrated_probability * odds - 1.0 if odds > 1.0 else 0.0
        reliable_ev = max(0.0, calibrated_ev) * reliability

        return ProbabilityCalibration(
            version=PROBABILITY_CALIBRATION_VERSION,
            raw_probability=raw_probability,
            implied_probability=implied,
            calibrated_probability=calibrated_probability,
            raw_edge=raw_edge,
            calibrated_edge=calibrated_edge,
            raw_ev=raw_ev,
            calibrated_ev=calibrated_ev,
            reliable_ev=reliable_ev,
            reliability=reliability,
            reliability_score=reliability * 100.0,
            support_confidence=support_conf,
            coverage_confidence=coverage,
            data_quality_confidence=quality_conf,
            penalty_confidence=penalty_conf,
            probability_shrinkage=max(0.0, raw_probability - calibrated_probability),
            capped_probability_advantage=capped_advantage,
        )
