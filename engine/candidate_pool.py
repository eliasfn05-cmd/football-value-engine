from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.db.models import Q
from django.utils import timezone

from .competition_quality import classify_competition
from .models import Prediction
from .score_v8 import V8_MODEL_VERSION
from .value_policy import PREMIUM_MIN_EV, is_premium_value_odds

# Sprint 7.4 — Professional Candidate Expansion
# These are discovery thresholds only. They deliberately sit below the final
# Premium floors so strong official senior fixtures can receive odds + rescore +
# Deep Analysis before being judged. Final Premium gates remain unchanged.
DISCOVERY_MIN_SCORE = 68.0
DISCOVERY_BTTS_PROBABILITY = 0.54
DISCOVERY_OVER25_PROBABILITY = 0.56
DISCOVERY_NEAR_SCORE = 64.0
DISCOVERY_DATA_QUALITY = 55.0


@dataclass(frozen=True)
class CandidatePoolRule:
    # Discovery must favor recall; the expensive final selector remains strict.
    min_score: float = DISCOVERY_MIN_SCORE
    min_edge: float = 0.04
    min_ev: float = float(PREMIUM_MIN_EV)
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

    # Odds/EV receive less weight during discovery because stale/missing prices
    # must not suppress statistically interesting official fixtures.
    composite = (
        0.34 * score
        + 0.24 * probability
        + 0.12 * edge_component
        + 0.13 * ev_component
        + 0.10 * data_quality
        + 0.07 * sample_confidence
    )
    if prediction.market_odds is None and float(prediction.probability or 0.0) >= _market_discovery_floor(prediction.market):
        composite += 10.0
    return round(composite, 3)


def high_recall_candidate_pool(
    target_date: date,
    *,
    rule: CandidatePoolRule | None = None,
    model_version: str = V8_MODEL_VERSION,
) -> list[CandidatePoolEntry]:
    """Return a broad but professional senior candidate pool.

    Sprint 7.4 intentionally widens *discovery* only. A fixture may enter because
    its probability is close enough to the final Premium floor, its raw score is
    promising, or it already carries value. Reserve/youth/women/lower-liquidity
    competitions remain excluded by ``classify_competition``. Final Premium
    odds, calibrated probability, Edge, Reliable EV, reliability and Deep gates
    are not relaxed here.
    """
    rule = rule or CandidatePoolRule()
    start, end = _bounds(target_date)
    future_start = max(start, timezone.now())

    # Pull a wider SQL universe, then apply market-specific discovery rules in
    # Python. 0.54 is the lowest market discovery probability (BTTS).
    predictions = (
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
            Q(score__gte=min(rule.min_score, DISCOVERY_NEAR_SCORE))
            | Q(edge__gte=rule.min_edge)
            | Q(expected_value__gte=rule.min_ev)
            | Q(probability__gte=DISCOVERY_BTTS_PROBABILITY)
        )
    )

    best_by_fixture: dict[int, CandidatePoolEntry] = {}
    for prediction in predictions.iterator(chunk_size=500):
        if classify_competition(prediction.fixture).excluded:
            continue

        probability = float(prediction.probability or 0.0)
        score = float(prediction.score or 0.0)
        reasons_data = prediction.reasons or {}
        data_quality = float(reasons_data.get("data_quality_score") or 0.0)
        market_floor = _market_discovery_floor(prediction.market)

        if rule.require_premium_value_odds:
            if not is_premium_value_odds(prediction.market_odds):
                continue
            if prediction.expected_value is None or float(prediction.expected_value) < rule.min_ev:
                continue
        else:
            # Professional expansion gate: being merely present in the SQL query
            # is not enough. Require at least one credible pre-Premium signal.
            near_probability = probability >= market_floor
            strong_score = score >= rule.min_score
            near_score_with_quality = (
                score >= DISCOVERY_NEAR_SCORE
                and probability >= market_floor - 0.02
                and data_quality >= DISCOVERY_DATA_QUALITY
            )
            existing_value = (
                (prediction.edge is not None and float(prediction.edge) >= rule.min_edge)
                or (prediction.expected_value is not None and float(prediction.expected_value) >= rule.min_ev)
            )
            if not (near_probability or strong_score or near_score_with_quality or existing_value):
                continue

        reasons: list[str] = []
        if score >= rule.min_score:
            reasons.append("score")
        if probability >= market_floor:
            reasons.append("near_premium_probability")
        if prediction.edge is not None and float(prediction.edge) >= rule.min_edge:
            reasons.append("edge")
        if prediction.expected_value is not None and float(prediction.expected_value) >= rule.min_ev:
            reasons.append("ev")
        if score >= DISCOVERY_NEAR_SCORE and data_quality >= DISCOVERY_DATA_QUALITY:
            reasons.append("professional_near_score")
        if prediction.market_odds is None and probability >= market_floor:
            reasons.append("missing_odds_candidate")
        if not reasons:
            continue

        entry = CandidatePoolEntry(
            fixture_id=prediction.fixture_id,
            prediction_id=prediction.id,
            preliminary_score=_preliminary_score(prediction),
            entry_reasons=tuple(reasons),
        )
        previous = best_by_fixture.get(prediction.fixture_id)
        if previous is None or entry.preliminary_score > previous.preliminary_score:
            best_by_fixture[prediction.fixture_id] = entry

    ranked = sorted(
        best_by_fixture.values(),
        key=lambda item: (item.preliminary_score, len(item.entry_reasons)),
        reverse=True,
    )
    return ranked[: max(1, int(rule.limit))]
