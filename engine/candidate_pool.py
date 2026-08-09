from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.db.models import Q
from django.utils import timezone

from .competition_quality import classify_competition
from .models import Prediction
from .score_v8 import V8_MODEL_VERSION


@dataclass(frozen=True)
class CandidatePoolRule:
    # Sprint 6.9: discovery should maximize recall. Expensive enrichment can still
    # consume a smaller top slice of this ranked pool.
    min_score: float = 78.0
    min_edge: float = 0.05
    min_ev: float = 0.06
    limit: int = 60


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
        0.36 * score
        + 0.20 * probability
        + 0.18 * edge_component
        + 0.17 * ev_component
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

    Entry is score OR edge OR EV, never a single score-only cutoff. One market is
    kept per fixture at this stage; official competition filtering remains hard.
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
