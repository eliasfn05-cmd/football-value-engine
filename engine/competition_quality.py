from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from .models import Fixture


@dataclass(frozen=True)
class CompetitionQuality:
    level: int
    label: str
    quality_score: float
    excluded: bool
    reason: str


FRIENDLY_TERMS = (
    "friendly",
    "friendlies",
    "amistoso",
    "amistosos",
    "exhibition",
    "exhibicion",
    "test match",
    "preseason",
    "pre season",
    "pretemporada",
    "legends match",
    "legend match",
    "all star",
    "soccer aid",
    "charity match",
    "benefit match",
)

TIER1_TERMS = (
    "champions league",
    "europa league",
    "conference league",
    "copa libertadores",
    "copa sudamericana",
    "premier league",
    "la liga",
    "laliga",
    "serie a",
    "bundesliga",
    "ligue 1",
    "brasileirao",
    "brasileirao serie a",
    "world cup",
    "copa america",
    "euro championship",
    "uefa european championship",
)

TIER3_TERMS = (
    "u17",
    "u18",
    "u19",
    "u20",
    "u21",
    "u23",
    "under 17",
    "under 18",
    "under 19",
    "under 20",
    "under 21",
    "under 23",
    "youth",
    "juvenil",
    "reserve",
    "reserves",
    "amateur",
    "development league",
)


def _normalize(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    normalized_terms = tuple(_normalize(term) for term in terms)
    return any(term and term in text for term in normalized_terms)


def classify_competition(fixture: Fixture) -> CompetitionQuality:
    competition = fixture.competition_ref
    name = fixture.competition or (competition.name if competition else "")
    competition_type = competition.competition_type if competition else ""
    country = competition.country if competition else ""
    text = _normalize(f"{name} {competition_type} {country}")

    if _contains_any(text, FRIENDLY_TERMS) or _normalize(competition_type) in {
        "friendly",
        "friendlies",
        "exhibition",
    }:
        return CompetitionQuality(
            level=4,
            label="TIER_4_EXCLUDED",
            quality_score=0.0,
            excluded=True,
            reason="friendly_or_exhibition",
        )

    if _contains_any(text, TIER3_TERMS):
        return CompetitionQuality(
            level=3,
            label="TIER_3_DEVELOPMENT",
            quality_score=65.0,
            excluded=False,
            reason="youth_reserve_or_amateur",
        )

    if _contains_any(text, TIER1_TERMS):
        return CompetitionQuality(
            level=1,
            label="TIER_1_ELITE",
            quality_score=100.0,
            excluded=False,
            reason="elite_official_competition",
        )

    return CompetitionQuality(
        level=2,
        label="TIER_2_OFFICIAL",
        quality_score=85.0,
        excluded=False,
        reason="official_standard_competition",
    )
