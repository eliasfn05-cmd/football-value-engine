from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.db.models import Q
from django.utils import timezone

from .competition_quality import classify_competition
from .models import Prediction
from .score_v8 import V8_MODEL_VERSION
from .value_policy import PREMIUM_MIN_EV, is_premium_value_odds


@dataclass(frozen=True)
class CandidatePoolRule:
    # Discovery can remain broad, but expensive Deep Analysis may request the
    # Premium Value band explicitly so low odds never consume scarce slots.
    min_score: float = 78.0
    min_edge: float = 0.05
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

    composite = (
        0.30 * score
        + 0.15 * probability
        + 0.22 * edge_component
        + 0.24 * ev_component
        + 0.05 * data_quality
        + 0.04 * sample_confidence
    )
    return round(composite, 3)


def high_recall_candidate_pool(
    target_date: date,
    *,
    rule: CandidatePoolRule | None = None,
    model_version: str = V8_MODEL_VERSION,
) -> list[CandidatePoolEntry]:
    """Return a diverse high-recall fixture pool.

    One market is kept per fixture. When ``require_premium_value_odds`` is true,
    only quoted markets inside 1.60-2.40 with minimum positive value enter the
    expensive enrichment/Deep Analysis queue.
    """
    rule = rule or CandidatePoolRule()
    start, end = _bounds(target_date)
    future_start = max(start, timezone.now())

    predictions = (
        Prediction.objects.select_related("fixture", "fixture__competition_ref")
        .filter(
            model_version=model_version,
            fixture__kickoff__gte=future_start,
            fixture__kickoff__lt=end,
        )
        .filter(
            Q(score__gte=rule.min_score)
            | Q(edge__gte=rule.min_edge)
            | Q(expected_value__gte=rule.min_ev)
        )
    )

    best_by_fixture: dict[int, CandidatePoolEntry] = {}
    for prediction in predictions.iterator(chunk_size=500):
        if classify_competition(prediction.fixture).excluded:
            continue
        if rule.require_premium_value_odds:
            if not is_premium_value_odds(prediction.market_odds):
                continue
            if prediction.expected_value is None or float(prediction.expected_value) < rule.min_ev:
                continue

        reasons: list[str] = []
        if float(prediction.score or 0.0) >= rule.min_score:
            reasons.append("score")
        if prediction.edge is not None and float(prediction.edge) >= rule.min_edge:
            reasons.append("edge")
        if prediction.expected_value is not None and float(prediction.expected_value) >= rule.min_ev:
            reasons.append("ev")
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
