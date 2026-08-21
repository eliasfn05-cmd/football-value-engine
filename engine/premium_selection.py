from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .competition_quality import classify_competition
from .deep_analysis import DEEP_ANALYSIS_VERSION
from .models import DailyPremiumSelection, Prediction
from .premium_risk_guard import PremiumRiskGuard
from .probability_calibration import (
    PREMIUM_MIN_RELIABILITY,
    TIER_A_MIN_RELIABILITY,
    ProbabilityEVCalibrationService,
)
from .score_v8 import V8_MODEL_VERSION
from .value_policy import PREMIUM_MIN_EV, TIER_A_MIN_EV, is_premium_value_odds


PREMIUM_MARKET = "BTTS"


@dataclass(frozen=True)
class TierRule:
    name: str
    min_score: float
    min_edge: float
    min_ev: float
    min_probability: float
    min_reliability: float


TIER_RULES = (
    TierRule("A", 90.0, 0.07, float(TIER_A_MIN_EV), 0.60, TIER_A_MIN_RELIABILITY),
    TierRule("B", 84.0, 0.05, float(PREMIUM_MIN_EV), 0.56, PREMIUM_MIN_RELIABILITY),
)

DYNAMIC_SCORE_FLOORS = (84.0, 82.0, 80.0, 78.0, 76.0)
BTTS_BASE_PROBABILITY_FLOOR = 0.56
BTTS_MIN_CALIBRATED_PROBABILITY = 0.55
BTTS_MIN_EFFECTIVE_RELIABILITY = 0.85
BTTS_MIN_FINAL_RANK = 75.0

DISAGREEMENT_FREE_GAP = 0.12
DISAGREEMENT_MAX_GAP = 0.30
DISAGREEMENT_MAX_RELIABILITY_PENALTY = 0.10
DISAGREEMENT_MAX_RANK_PENALTY = 8.0

VENUE_RECENT_MIN_SAMPLE = 5
VENUE_WEAK_RECENT_RATE = 0.40
VENUE_WEAK_LONG_RATE = 0.50
VENUE_STRONG_RECENT_RATE = 0.70
VENUE_SOFT_TARGET = 0.60
VENUE_MAX_RELIABILITY_PENALTY = 0.06
VENUE_MAX_RANK_PENALTY = 7.0


class DailyPremiumSelector:
    """Select up to three BTTS-YES Premium Value picks only."""

    calibrator = ProbabilityEVCalibrationService()

    def __init__(self, model_version: str = V8_MODEL_VERSION, max_picks: int = 3):
        self.model_version = model_version
        self.max_picks = max(1, min(int(max_picks), 3))

    @staticmethod
    def _bounds(target_date: date):
        start = timezone.make_aware(datetime.combine(target_date, time.min))
        return start, start + timedelta(days=1)

    @staticmethod
    def _probability_floor(rule: TierRule, market: str = PREMIUM_MARKET) -> float:
        return rule.min_probability if market == PREMIUM_MARKET else 1.0

    @staticmethod
    def _base_probability_floor(market: str) -> float:
        return BTTS_BASE_PROBABILITY_FLOOR if market == PREMIUM_MARKET else 1.0

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
    def _venue_contradiction_metrics(cls, prediction: Prediction) -> dict:
        if prediction.market != PREMIUM_MARKET:
            return {"blocked": True, "severity": 1.0, "reliability_penalty": VENUE_MAX_RELIABILITY_PENALTY, "rank_penalty": VENUE_MAX_RANK_PENALTY, "weak_side": None, "reason": "btts_only_market"}
        evidence = (prediction.reasons or {}).get("deep_analysis_evidence") or {}
        try:
            home_n = int(evidence.get("home_recent_n") or 0)
            away_n = int(evidence.get("away_recent_n") or 0)
            home_long = float(evidence.get("home_btts_rate"))
            away_long = float(evidence.get("away_btts_rate"))
            home_recent = float(evidence.get("home_recent_btts_rate"))
            away_recent = float(evidence.get("away_recent_btts_rate"))
        except (TypeError, ValueError):
            return {"blocked": True, "severity": 1.0, "reliability_penalty": VENUE_MAX_RELIABILITY_PENALTY, "rank_penalty": VENUE_MAX_RANK_PENALTY, "weak_side": None, "reason": "btts_venue_evidence_missing"}
        if home_n < VENUE_RECENT_MIN_SAMPLE or away_n < VENUE_RECENT_MIN_SAMPLE:
            return {"blocked": True, "severity": 1.0, "reliability_penalty": VENUE_MAX_RELIABILITY_PENALTY, "rank_penalty": VENUE_MAX_RANK_PENALTY, "weak_side": None, "reason": "btts_venue_sample_incomplete"}
        if home_recent <= away_recent:
            weak_side = "home"; weak_recent, weak_long, strong_recent = home_recent, home_long, away_recent
        else:
            weak_side = "away"; weak_recent, weak_long, strong_recent = away_recent, away_long, home_recent
        shortfall = max(0.0, VENUE_SOFT_TARGET - weak_recent)
        severity = min(1.0, shortfall / VENUE_SOFT_TARGET)
        reliability_penalty = severity * VENUE_MAX_RELIABILITY_PENALTY
        rank_penalty = severity * VENUE_MAX_RANK_PENALTY
        blocked = weak_recent <= VENUE_WEAK_RECENT_RATE and weak_long <= VENUE_WEAK_LONG_RATE and strong_recent >= VENUE_STRONG_RECENT_RATE
        try:
            weak_fts = float(evidence.get("home_recent_failed_to_score_rate" if weak_side == "home" else "away_recent_failed_to_score_rate"))
        except (TypeError, ValueError):
            weak_fts = 0.0
        if weak_recent <= VENUE_WEAK_RECENT_RATE and weak_fts >= 0.40 and strong_recent >= 0.60:
            blocked = True
        return {"blocked": blocked, "severity": round(severity, 4), "reliability_penalty": round(reliability_penalty, 4), "rank_penalty": round(rank_penalty, 2), "weak_side": weak_side, "weak_recent_rate": round(weak_recent, 3), "weak_long_rate": round(weak_long, 3), "strong_recent_rate": round(strong_recent, 3)}

    @classmethod
    def _market_eligible_deep_preference(cls, prediction: Prediction) -> bool:
        return prediction.market == PREMIUM_MARKET

    @classmethod
    def _passes_hard_value_floors(cls, prediction: Prediction) -> bool:
        if prediction.market != PREMIUM_MARKET:
            return False
        if classify_competition(prediction.fixture).excluded:
            return False
        # IMPORTANT: enforce the runtime risk policy in the selector itself.
        # This makes V2/V2.1 league, audit, H2H and asymmetric-scoring vetoes
        # impossible to bypass through ranking/rescue/publication paths.
        if PremiumRiskGuard.evaluate(prediction).blocked:
            return False
        reasons = prediction.reasons or {}
        if not reasons.get("v8_gates_passed", False):
            return False
        if reasons.get("deep_analysis_version") != DEEP_ANALYSIS_VERSION or reasons.get("deep_analysis_passed") is not True:
            return False
        if prediction.market_odds is None or prediction.edge is None or prediction.expected_value is None:
            return False
        if not is_premium_value_odds(prediction.market_odds):
            return False
        calibration = cls.calibrator.calibrate(prediction)
        disagreement = cls._disagreement_metrics(prediction)
        venue = cls._venue_contradiction_metrics(prediction)
        if not calibration.premium_reliable:
            return False
        effective_reliability = max(0.0, disagreement["effective_reliability"] - venue["reliability_penalty"])
        # V2.2 core floor: no monkey-patch dependency.
        if effective_reliability < BTTS_MIN_EFFECTIVE_RELIABILITY:
            return False
        if venue["blocked"]:
            return False
        if calibration.raw_probability < BTTS_BASE_PROBABILITY_FLOOR:
            return False
        if calibration.calibrated_probability < BTTS_MIN_CALIBRATED_PROBABILITY:
            return False
        if calibration.calibrated_edge < 0.05:
            return False
        if calibration.reliable_ev < float(PREMIUM_MIN_EV):
            return False
        return True

    @classmethod
    def rejection_reasons(cls, prediction: Prediction, *, score_floor: float = 76.0) -> list[str]:
        if prediction.market != PREMIUM_MARKET:
            return ["market:btts_only"]
        out: list[str] = []
        quality = classify_competition(prediction.fixture)
        if quality.excluded:
            return [f"competition:{quality.reason}"]
        risk = PremiumRiskGuard.evaluate(prediction)
        if risk.blocked:
            out.append(f"risk_guard:{risk.code}:{risk.detail}")
        reasons = prediction.reasons or {}
        if not reasons.get("v8_gates_passed", False): out.append("v8_gates")
        if reasons.get("deep_analysis_version") != DEEP_ANALYSIS_VERSION: out.append("deep_missing")
        elif reasons.get("deep_analysis_passed") is not True: out.append("deep_rejected")
        if prediction.market_odds is None:
            out.append("no_odds"); return out
        if not is_premium_value_odds(prediction.market_odds): out.append("odds_outside_1.60_2.40")
        calibration = cls.calibrator.calibrate(prediction)
        disagreement = cls._disagreement_metrics(prediction)
        venue = cls._venue_contradiction_metrics(prediction)
        effective_reliability = max(0.0, disagreement["effective_reliability"] - venue["reliability_penalty"])
        if effective_reliability < BTTS_MIN_EFFECTIVE_RELIABILITY: out.append(f"effective_reliability_v22:{effective_reliability:.3f}")
        if venue["blocked"]: out.append(f"venue_btts_contradiction:{venue.get('reason') or venue.get('weak_side')}")
        if calibration.raw_probability < BTTS_BASE_PROBABILITY_FLOOR: out.append(f"raw_probability:{calibration.raw_probability:.3f}")
        if calibration.calibrated_probability < BTTS_MIN_CALIBRATED_PROBABILITY: out.append(f"calibrated_probability:{calibration.calibrated_probability:.3f}")
        if calibration.calibrated_edge < 0.05: out.append(f"calibrated_edge:{calibration.calibrated_edge:.3f}")
        if calibration.reliable_ev < float(PREMIUM_MIN_EV): out.append(f"reliable_ev:{calibration.reliable_ev:.3f}")
        if float(prediction.score or 0.0) < score_floor: out.append(f"score:{float(prediction.score or 0.0):.1f}")
        return out

    @classmethod
    def _tier_for(cls, prediction: Prediction, *, score_floor: float = 84.0) -> str | None:
        if not cls._passes_hard_value_floors(prediction): return None
        calibration = cls.calibrator.calibrate(prediction)
        disagreement = cls._disagreement_metrics(prediction)
        venue = cls._venue_contradiction_metrics(prediction)
        score = float(prediction.score or 0.0)
        reliability = max(0.0, disagreement["effective_reliability"] - venue["reliability_penalty"])
        for rule in TIER_RULES:
            effective_score = rule.min_score if rule.name == "A" else min(rule.min_score, score_floor)
            if score >= effective_score and calibration.calibrated_edge >= rule.min_edge and calibration.reliable_ev >= rule.min_ev and calibration.raw_probability >= rule.min_probability and calibration.calibrated_probability >= BTTS_MIN_CALIBRATED_PROBABILITY and reliability >= rule.min_reliability:
                return rule.name
        return None

    @classmethod
    def _rank_score(cls, prediction: Prediction) -> tuple[float, dict]:
        calibration = cls.calibrator.calibrate(prediction)
        disagreement = cls._disagreement_metrics(prediction)
        venue = cls._venue_contradiction_metrics(prediction)
        score_component = max(0.0, min(float(prediction.score or 0.0), 100.0))
        probability_component = calibration.raw_probability * 100.0
        calibrated_probability_component = calibration.calibrated_probability * 100.0
        ev_component = min(max(0.0, calibration.reliable_ev) / 0.20, 1.0) * 100.0
        edge_component = min(max(0.0, calibration.calibrated_edge) / 0.12, 1.0) * 100.0
        effective_reliability = max(0.0, disagreement["effective_reliability"] - venue["reliability_penalty"])
        reliability_component = effective_reliability * 100.0
        base_composite = 0.18*score_component + 0.25*ev_component + 0.18*edge_component + 0.14*probability_component + 0.10*calibrated_probability_component + 0.15*reliability_component
        composite = max(0.0, base_composite - disagreement["rank_penalty"] - venue["rank_penalty"])
        reasons = prediction.reasons or {}; evidence = reasons.get("deep_analysis_evidence") or {}
        rationale = {"operational_market": PREMIUM_MARKET, "score_component": round(score_component,2), "raw_probability_component": round(probability_component,2), "calibrated_probability_component": round(calibrated_probability_component,2), "ev_component": round(ev_component,2), "edge_component": round(edge_component,2), "reliability_component": round(reliability_component,2), "deep_analysis_version": reasons.get("deep_analysis_version"), "deep_analysis_evidence": evidence, "deep_analysis_warnings": reasons.get("deep_analysis_warnings") or [], "deep_score": reasons.get("deep_score"), "probability_calibration": calibration.as_dict(), "raw_probability": calibration.raw_probability, "calibrated_probability": calibration.calibrated_probability, "market_implied_probability": calibration.implied_probability, "market_odds": float(prediction.market_odds) if prediction.market_odds is not None else None, "raw_edge": calibration.raw_edge, "calibrated_edge": calibration.calibrated_edge, "raw_expected_value": calibration.raw_ev, "calibrated_expected_value": calibration.calibrated_ev, "reliable_expected_value": calibration.reliable_ev, "probability_reliability": calibration.reliability, "effective_reliability_after_disagreement": round(disagreement["effective_reliability"],4), "effective_reliability_after_venue": round(effective_reliability,4), "model_market_probability_gap": round(disagreement["gap"],4), "venue_specific_contradiction": venue, "rank_before_penalties": round(base_composite,2), "over_context_only": {"home_over25_rate": evidence.get("home_over25_rate"), "away_over25_rate": evidence.get("away_over25_rate"), "home_avg_total_goals": evidence.get("home_avg_total_goals"), "away_avg_total_goals": evidence.get("away_avg_total_goals")}, "odds_policy": "Premium Value 1.60-2.40", "value_gate": "BTTS-only: risk guard + Deep + venue + calibrated edge/EV + reliability", "formula": "0.18*score + 0.25*reliable_ev + 0.18*edge + 0.14*raw_p + 0.10*cal_p + 0.15*reliability - penalties"}
        return round(composite, 2), rationale

    def _rank_candidates(self, candidates: list[Prediction], score_floor: float):
        ranked = []
        for prediction in candidates:
            if prediction.market != PREMIUM_MARKET or classify_competition(prediction.fixture).excluded: continue
            tier = self._tier_for(prediction, score_floor=score_floor)
            if tier is None: continue
            rank_score, rationale = self._rank_score(prediction)
            # V2.2 final publication floor lives in the selector core too.
            if rank_score < BTTS_MIN_FINAL_RANK: continue
            rationale["premium_tier"] = tier; rationale["dynamic_score_floor"] = score_floor
            ranked.append((prediction, tier, rank_score, rationale))
        tier_priority = {"A":2,"B":1}
        ranked.sort(key=lambda item:(tier_priority[item[1]], item[2], float(item[3].get("reliable_expected_value") or 0), float(item[0].score or 0.0)), reverse=True)
        return ranked

    @staticmethod
    def _unique_fixture_count(ranked) -> int:
        return len({item[0].fixture_id for item in ranked})

    @transaction.atomic
    def select(self, target_date: date) -> list[DailyPremiumSelection]:
        start, end = self._bounds(target_date); future_start = max(start, timezone.now())
        candidates = list(Prediction.objects.select_related("fixture","fixture__home_team","fixture__away_team","fixture__competition_ref").filter(model_version=self.model_version, market=PREMIUM_MARKET, fixture__kickoff__gte=future_start, fixture__kickoff__lt=end, market_odds__gte=Decimal("1.60"), market_odds__lte=Decimal("2.40"), edge__isnull=False, expected_value__gte=PREMIUM_MIN_EV))
        ranked=[]; selected_floor=DYNAMIC_SCORE_FLOORS[-1]
        for score_floor in DYNAMIC_SCORE_FLOORS:
            ranked=self._rank_candidates(candidates,score_floor); selected_floor=score_floor
            if self._unique_fixture_count(ranked)>=self.max_picks: break
        chosen=[]; seen_fixtures=set()
        for item in ranked:
            if item[0].fixture_id in seen_fixtures: continue
            chosen.append(item); seen_fixtures.add(item[0].fixture_id)
            if len(chosen)>=self.max_picks: break
        DailyPremiumSelection.objects.filter(target_date=target_date, model_version=self.model_version).delete()
        rows=[]
        for index,(prediction,tier,rank_score,rationale) in enumerate(chosen,start=1):
            rationale["selector_dynamic_floor_used"]=selected_floor
            rows.append(DailyPremiumSelection(target_date=target_date,prediction=prediction,rank=index,premium_tier=tier,premium_rank_score=Decimal(f"{rank_score:.2f}"),model_version=self.model_version,rationale=rationale))
        if rows: DailyPremiumSelection.objects.bulk_create(rows)
        return list(DailyPremiumSelection.objects.select_related("prediction","prediction__fixture","prediction__fixture__home_team","prediction__fixture__away_team","prediction__fixture__competition_ref").filter(target_date=target_date,model_version=self.model_version).order_by("rank"))