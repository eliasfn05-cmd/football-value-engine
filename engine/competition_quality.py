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
    "friendly", "friendlies", "friendly games", "club friendly", "club friendlies",
    "friendlies clubs", "international friendly", "international friendlies",
    "amistoso", "amistosos", "exhibition", "exhibicion", "test match",
    "preseason", "pre season", "pretemporada", "legends match", "legend match",
    "all star", "soccer aid", "charity match", "benefit match",
)

TIER1_TERMS = (
    "champions league", "europa league", "conference league", "copa libertadores",
    "copa sudamericana", "premier league", "la liga", "laliga", "serie a",
    "bundesliga", "ligue 1", "brasileirao", "brasileirao serie a", "world cup",
    "copa america", "euro championship", "uefa european championship",
)

# Sprint 7.4 quality gate: these competitions/teams are not allowed to consume
# model, odds or Deep Analysis capacity. The system is intentionally focused on
# professional senior men's football with stronger liquidity/data quality.
DEVELOPMENT_TERMS = (
    "u17", "u18", "u19", "u20", "u21", "u22", "u23",
    "under 17", "under 18", "under 19", "under 20", "under 21", "under 22", "under 23",
    "youth", "juvenil", "reserve", "reserves", "development league", "academy",
    "segunda b", "segunda division b", "serie d", "tercera division", "third division",
    "fourth division", "regional league", "amateur", "semi professional", "semiprofessional",
)

WOMEN_TERMS = (
    "women", "womens", "women s", "female", "femenino", "femenina", "femminile",
    "frauen", "dames", "ladies", "w league", "nwsl", "liga f", "superliga femenina",
)

# Obvious lower-league labels. We deliberately avoid generic words such as
# "division 2" alone because several countries use unconventional naming for
# their top flight; the terms below are high-confidence lower-tier signals.
LOWER_LEAGUE_TERMS = (
    "serie c", "serie c group", "serie c girone", "serie d",
    "league two", "liga 3", "liga iii", "third league", "3 liga", "3rd liga",
    "segunda federacion", "tercera federacion", "national league north", "national league south",
    "primera b metropolitana", "primera c", "primera d",
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
    fixture_name = fixture.competition or ""
    ref_name = competition.name if competition else ""
    competition_type = competition.competition_type if competition else ""
    country = competition.country if competition else ""
    round_name = fixture.round or ""
    external_id = competition.external_id if competition else ""
    home_name = getattr(getattr(fixture, "home_team", None), "name", "") or ""
    away_name = getattr(getattr(fixture, "away_team", None), "name", "") or ""

    text = _normalize(
        f"{fixture_name} {ref_name} {competition_type} {country} {round_name} {external_id} {home_name} {away_name}"
    )

    if _contains_any(text, FRIENDLY_TERMS) or _normalize(competition_type) in {"friendly", "friendlies", "exhibition"}:
        return CompetitionQuality(4, "TIER_4_EXCLUDED", 0.0, True, "friendly_or_exhibition")

    if _contains_any(text, WOMEN_TERMS):
        return CompetitionQuality(4, "TIER_4_EXCLUDED", 0.0, True, "women_competition")

    if _contains_any(text, DEVELOPMENT_TERMS):
        return CompetitionQuality(4, "TIER_4_EXCLUDED", 0.0, True, "youth_reserve_amateur_or_development")

    if _contains_any(text, LOWER_LEAGUE_TERMS):
        return CompetitionQuality(4, "TIER_4_EXCLUDED", 0.0, True, "lower_league_liquidity_filter")

    if _contains_any(text, TIER1_TERMS):
        return CompetitionQuality(1, "TIER_1_ELITE", 100.0, False, "elite_official_competition")

    return CompetitionQuality(2, "TIER_2_OFFICIAL", 85.0, False, "official_standard_competition")
