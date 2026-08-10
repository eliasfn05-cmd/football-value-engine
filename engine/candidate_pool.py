from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.utils import timezone

from .competition_quality import classify_competition
from .models import Prediction
from .score_v8 import V8_MODEL_VERSION
from .value_policy import is_premium_value_odds

# Sprint 7.8.1 — Broad Professional Recall, guaranteed fill.
# Discovery is deliberately wider than Premium admission. We first keep the
# strongest near-Premium signals and then fill with the best remaining official
# senior fixtures until the pool reaches 24-30 where the schedule allows it.
# Final Premium gates are unchanged in premium_selection.py.
DISCOVERY_MIN_SCORE = 64.0
DISCOVERY_BTTS_PROBABILITY = 0.50
DISCOVERY_OVER25_PROBABILITY = 0.52
DISCOVERY_NEAR_SCORE = 58.0
DISCOVERY_DATA_QUALITY = 45.0
DISCOVERY_SOFT_BTTS = 0.47
DISCOVERY_SOFT_OVER25 = 0.49
DISCOVERY_SOFT_SCORE = 54.0
DISCOVERY_BASELINE_PROBABILITY = 0.42
DISCOVERY_BASELINE_SCORE = 45.0
DISCOVERY_BASELINE_DATA_QUALITY = 35.0
DISCOVERY_MIN_FIXTURES = 24
DISCOVERY_TARGET_FIXTURES = 30


@dataclass(frozen=True)
class CandidatePoolRule:
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
    score = max(0.0, min(float(prediction.score or 0.0), 100.0))
    probability = max(0.0, min(float(prediction.probability or 0.0), 1.0)) * 100.0
    edge = max(0.0, float(prediction.edge or 0.0))
    ev = max(0.0, float(prediction.expected_value or 0.0))
    edge_component = min(edge / 0.15, 1.0) * 100.0
    ev_component = min(ev / 0.25, 1.0) * 100.0
    reasons = prediction.reasons or {}
    data_quality = max(0.0, min(float(reasons.get("data_quality_score") or 0.0), 100.0))
    sample_confidence = max(0.0, min(float(reasons.get("venue_sample_confidence") or 0.0), 1.0)) * 100.0

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


def _entry_for_prediction(
    prediction: Prediction,
    rule: CandidatePoolRule,
    *,
    broad_fill: bool = False,
    baseline_fill: bool = False,
) -> CandidatePoolEntry | None:
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
        baseline_professional_signal = baseline_fill and (
            probability >= DISCOVERY_BASELINE_PROBABILITY
            or score >= DISCOVERY_BASELINE_SCORE
            or data_quality >= DISCOVERY_BASELINE_DATA_QUALITY
        )
        if not (
            near_probability
            or strong_score
            or near_score_with_quality
            or existing_value
            or broad_professional_signal
            or baseline_professional_signal
        ):
            return None

    reasons: list[str] = []
    if score >= rule.min_score:
        reasons.append("score")
    if probability >= market_floor:
        reasons.append("near_premium_probability")
    elif broad_fill and probability >= soft_floor:
        reasons.append("broad_probability_recall")
    elif baseline_fill and probability >= DISCOVERY_BASELINE_PROBABILITY:
        reasons.append("baseline_probability_recall")
    if prediction.edge is not None and float(prediction.edge) >= rule.min_edge:
        reasons.append("edge")
    if prediction.expected_value is not None and float(prediction.expected_value) >= rule.min_ev:
        reasons.append("ev")
    if score >= DISCOVERY_NEAR_SCORE and data_quality >= DISCOVERY_DATA_QUALITY:
        reasons.append("professional_near_score")
    if broad_fill and score >= DISCOVERY_SOFT_SCORE:
        reasons.append("broad_professional_recall")
    if baseline_fill and (
        score >= DISCOVERY_BASELINE_SCORE or data_quality >= DISCOVERY_BASELINE_DATA_QUALITY
    ):
        reasons.append("senior_baseline_recall")
    if prediction.market_odds is None and probability >= DISCOVERY_BASELINE_PROBABILITY:
        reasons.append("missing_odds_candidate")
    if not reasons:
        return None

    return CandidatePoolEntry(
        fixture_id=prediction.fixture_id,
        prediction_id=prediction.id,
        preliminary_score=_preliminary_score(prediction),
        entry_reasons=tuple(dict.fromkeys(reasons)),
    )


def _put_best(store: dict[int, CandidatePoolEntry], entry: CandidatePoolEntry) -> None:
    previous = store.get(entry.fixture_id)
    if previous is None or entry.preliminary_score > previous.preliminary_score:
        store[entry.fixture_id] = entry


def high_recall_candidate_pool(
    target_date: date,
    *,
    rule: CandidatePoolRule | None = None,
    model_version: str = V8_MODEL_VERSION,
) -> list[CandidatePoolEntry]:
    """Return a broad official-senior candidate pool for pre-Premium research.

    Sprint 7.8.1 uses three passes over all BTTS/Over predictions for the date:
    1) near-Premium signals;
    2) softer professional recall;
    3) guaranteed senior baseline fill toward 24-30 unique fixtures.

    The third pass fixes the Sprint 7.8 issue where the SQL pre-filter itself
    prevented the pool from ever reaching the requested recall floor. Excluded
    competitions remain excluded, and none of the final Premium gates change.
    """
    rule = rule or CandidatePoolRule()
    start, end = _bounds(target_date)
    future_start = max(start, timezone.now())

    # Important: do NOT pre-filter by probability/score here. Doing so was the
    # hidden bottleneck in 7.8. We need the full official-senior universe for the
    # baseline fill, then we rank it cheaply in Python before expensive odds/Deep.
    predictions = list(
        Prediction.objects.select_related(
            "fixture",
            "fixture__competition_ref",
            "fixture__home_team",
            "fixture__away_team",
        ).filter(
            model_version=model_version,
            fixture__kickoff__gte=future_start,
            fixture__kickoff__lt=end,
            market__in=("BTTS", "OVER_2_5"),
        )
    )

    best_by_fixture: dict[int, CandidatePoolEntry] = {}

    for prediction in predictions:
        entry = _entry_for_prediction(prediction, rule)
        if entry is not None:
            _put_best(best_by_fixture, entry)

    target = min(DISCOVERY_TARGET_FIXTURES, max(1, int(rule.limit)))
    minimum = min(DISCOVERY_MIN_FIXTURES, target)

    if len(best_by_fixture) < target and not rule.require_premium_value_odds:
        broad_candidates: list[CandidatePoolEntry] = []
        for prediction in predictions:
            entry = _entry_for_prediction(prediction, rule, broad_fill=True)
            if entry is not None:
                broad_candidates.append(entry)
        broad_candidates.sort(
            key=lambda item: (item.preliminary_score, len(item.entry_reasons)),
            reverse=True,
        )
        for entry in broad_candidates:
            _put_best(best_by_fixture, entry)
            if len(best_by_fixture) >= target:
                break

    # Guaranteed senior baseline fill. This pass is intentionally permissive but
    # still excludes reserves/youth/women/lower-quality competitions through
    # classify_competition. It only decides who receives odds/rescore; it cannot
    # create a Premium selection by itself.
    if len(best_by_fixture) < minimum and not rule.require_premium_value_odds:
        baseline_candidates: list[CandidatePoolEntry] = []
        for prediction in predictions:
            entry = _entry_for_prediction(prediction, rule, baseline_fill=True)
            if entry is not None:
                baseline_candidates.append(entry)
        baseline_candidates.sort(
            key=lambda item: (item.preliminary_score, len(item.entry_reasons)),
            reverse=True,
        )
        for entry in baseline_candidates:
            _put_best(best_by_fixture, entry)
            if len(best_by_fixture) >= target:
                break

    ranked = sorted(
        best_by_fixture.values(),
        key=lambda item: (item.preliminary_score, len(item.entry_reasons)),
        reverse=True,
    )
    return ranked[: max(1, int(rule.limit))]
