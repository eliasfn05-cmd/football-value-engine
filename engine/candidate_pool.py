from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.db.models import Q
from django.utils import timezone

from .competition_quality import classify_competition
from .models import Prediction
from .score_v8 import V8_MODEL_VERSION
from .value_policy import PREMIUM_MIN_EV, is_premium_value_odds

# Sprint 7.8 — Broad Professional Recall
# Discovery is intentionally broader than Premium admission. The goal is to
# investigate more official senior fixtures with odds + rescore before rejecting
# them. Final Premium gates remain unchanged in premium_selection.py.
DISCOVERY_MIN_SCORE = 64.0
DISCOVERY_BTTS_PROBABILITY = 0.50
DISCOVERY_OVER25_PROBABILITY = 0.52
DISCOVERY_NEAR_SCORE = 58.0
DISCOVERY_DATA_QUALITY = 45.0
DISCOVERY_SOFT_BTTS = 0.47
DISCOVERY_SOFT_OVER25 = 0.49
DISCOVERY_SOFT_SCORE = 54.0
DISCOVERY_MIN_FIXTURES = 24
DISCOVERY_TARGET_FIXTURES = 30


@dataclass(frozen=True)
class CandidatePoolRule:
    # Discovery must favor recall; the expensive final selector remains strict.
    min_score: float = DISCOVERY_MIN_SCORE
    min_edge: float = 0.03
    min_ev: float = 0.02
    limit: int = 60
    require_premium_value_odds: bool = False


@dataclass(frozen=True)
class CandidatePoolEntry:
    fixture_id: int
    prediction_id: int
    preliminary_score: float
    entry_reasons: tuple[str, ...]


def _bounds(target_date: date):
    start = timezone.make_aware(datetime.combine(target_date, time.min))
    return start, start + timedelta(days=1)


def _market_discovery_floor(market: str) -> float:
    if market == "BTTS":
        return DISCOVERY_BTTS_PROBABILITY
    if market == "OVER_2_5":
        return DISCOVERY_OVER25_PROBABILITY
    return 1.0


def _market_soft_floor(market: str) -> float:
    if market == "BTTS":
        return DISCOVERY_SOFT_BTTS
    if market == "OVER_2_5":
        return DISCOVERY_SOFT_OVER25
    return 1.0


def _preliminary_score(prediction: Prediction) -> float:
    """Recall-oriented ranking used before expensive enrichment."""
    score = max(0.0, min(float(prediction.score or 0.0), 100.0))
    probability = max(0.0, min(float(prediction.probability or 0.0), 1.0)) * 100.0
    edge = max(0.0, float(prediction.edge or 0.0))
    ev = max(0.0, float(prediction.expected_value or 0.0))
    edge_component = min(edge / 0.15, 1.0) * 100.0
    ev_component = min(ev / 0.25, 1.0) * 100.0
    reasons = prediction.reasons or {}
    data_quality = max(0.0, min(float(reasons.get("data_quality_score") or 0.0), 100.0))
    sample_confidence = max(0.0, min(float(reasons.get("venue_sample_confidence") or 0.0), 1.0)) * 100.0

    # Sprint 7.8: probability + structural score dominate discovery. Odds/EV are
    # useful but cannot veto an unpriced official senior fixture.
    composite = (
        0.31 * score
        + 0.31 * probability
        + 0.09 * edge_component
        + 0.09 * ev_component
        + 0.12 * data_quality
        + 0.08 * sample_confidence
    )
    if prediction.market_odds is None and float(prediction.probability or 0.0) >= _market_discovery_floor(prediction.market):
        composite += 8.0
    return round(composite, 3)


def _entry_for_prediction(prediction: Prediction, rule: CandidatePoolRule, *, broad_fill: bool = False) -> CandidatePoolEntry | None:
    if classify_competition(prediction.fixture).excluded:
        return None

    probability = float(prediction.probability or 0.0)
    score = float(prediction.score or 0.0)
    reasons_data = prediction.reasons or {}
    data_quality = float(reasons_data.get("data_quality_score") or 0.0)
    market_floor = _market_discovery_floor(prediction.market)
    soft_floor = _market_soft_floor(prediction.market)

    if rule.require_premium_value_odds:
        if not is_premium_value_odds(prediction.market_odds):
            return None
        if prediction.expected_value is None or float(prediction.expected_value) < rule.min_ev:
            return None
    else:
        near_probability = probability >= market_floor
        strong_score = score >= rule.min_score
        near_score_with_quality = (
            score >= DISCOVERY_NEAR_SCORE
            and probability >= market_floor - 0.03
            and data_quality >= DISCOVERY_DATA_QUALITY
        )
        existing_value = (
            (prediction.edge is not None and float(prediction.edge) >= rule.min_edge)
            or (prediction.expected_value is not None and float(prediction.expected_value) >= rule.min_ev)
        )
        broad_professional_signal = broad_fill and (
            (probability >= soft_floor and score >= DISCOVERY_SOFT_SCORE)
            or (score >= DISCOVERY_NEAR_SCORE and data_quality >= DISCOVERY_DATA_QUALITY)
        )
        if not (near_probability or strong_score or near_score_with_quality or existing_value or broad_professional_signal):
            return None

    reasons: list[str] = []
    if score >= rule.min_score:
        reasons.append("score")
    if probability >= market_floor:
        reasons.append("near_premium_probability")
    elif broad_fill and probability >= soft_floor:
        reasons.append("broad_probability_recall")
    if prediction.edge is not None and float(prediction.edge) >= rule.min_edge:
        reasons.append("edge")
    if prediction.expected_value is not None and float(prediction.expected_value) >= rule.min_ev:
        reasons.append("ev")
    if score >= DISCOVERY_NEAR_SCORE and data_quality >= DISCOVERY_DATA_QUALITY:
        reasons.append("professional_near_score")
    if broad_fill and score >= DISCOVERY_SOFT_SCORE:
        reasons.append("broad_professional_recall")
    if prediction.market_odds is None and probability >= soft_floor:
        reasons.append("missing_odds_candidate")
    if not reasons:
        return None

    return CandidatePoolEntry(
        fixture_id=prediction.fixture_id,
        prediction_id=prediction.id,
        preliminary_score=_preliminary_score(prediction),
        entry_reasons=tuple(dict.fromkeys(reasons)),
    )


def high_recall_candidate_pool(
    target_date: date,
    *,
    rule: CandidatePoolRule | None = None,
    model_version: str = V8_MODEL_VERSION,
) -> list[CandidatePoolEntry]:
    """Return a broad official-senior candidate pool for pre-Premium research.

    Sprint 7.8 uses two passes:
    1) normal near-Premium discovery;
    2) if too few fixtures survive, a softer professional recall pass fills the
       pool toward 24-30 fixtures. Reserve/youth/women/lower-quality competitions
       remain excluded. Final odds, Deep, calibrated Edge/EV, reliability and
       Premium score gates are NOT relaxed here.
    """
    rule = rule or CandidatePoolRule()
    start, end = _bounds(target_date)
    future_start = max(start, timezone.now())

    predictions = list(
        Prediction.objects.select_related(
            "fixture",
            "fixture__competition_ref",
            "fixture__home_team",
            "fixture__away_team",
        )
        .filter(
            model_version=model_version,
            fixture__kickoff__gte=future_start,
            fixture__kickoff__lt=end,
            market__in=("BTTS", "OVER_2_5"),
        )
        .filter(
            Q(score__gte=DISCOVERY_SOFT_SCORE)
            | Q(edge__gte=rule.min_edge)
            | Q(expected_value__gte=rule.min_ev)
            | Q(probability__gte=min(DISCOVERY_SOFT_BTTS, DISCOVERY_SOFT_OVER25))
        )
    )

    best_by_fixture: dict[int, CandidatePoolEntry] = {}
    for prediction in predictions:
        entry = _entry_for_prediction(prediction, rule, broad_fill=False)
        if entry is None:
            continue
        previous = best_by_fixture.get(entry.fixture_id)
        if previous is None or entry.preliminary_score > previous.preliminary_score:
            best_by_fixture[entry.fixture_id] = entry

    # Broad professional fill: only activates when the strict discovery pass is
    # too narrow. This is the key Sprint 7.8 change that prevents 100+ fixtures
    # collapsing to 5-7 before odds are even inspected.
    target = min(max(DISCOVERY_MIN_FIXTURES, DISCOVERY_TARGET_FIXTURES), max(1, int(rule.limit)))
    if len(best_by_fixture) < target and not rule.require_premium_value_odds:
        broad_candidates: list[CandidatePoolEntry] = []
        for prediction in predictions:
            if prediction.fixture_id in best_by_fixture:
                continue
            entry = _entry_for_prediction(prediction, rule, broad_fill=True)
            if entry is not None:
                broad_candidates.append(entry)
        broad_candidates.sort(key=lambda item: (item.preliminary_score, len(item.entry_reasons)), reverse=True)
        for entry in broad_candidates:
            previous = best_by_fixture.get(entry.fixture_id)
            if previous is None or entry.preliminary_score > previous.preliminary_score:
                best_by_fixture[entry.fixture_id] = entry
            if len(best_by_fixture) >= target:
                break

    ranked = sorted(
        best_by_fixture.values(),
        key=lambda item: (item.preliminary_score, len(item.entry_reasons)),
        reverse=True,
    )
    return ranked[: max(1, int(rule.limit))]
