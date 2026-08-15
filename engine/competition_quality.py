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

OFFICIAL_SENIOR_TERMS = (
    "liga de primera", "primera division", "primera division chile", "liga chilena",
    "division de honor", "division profesional", "liga profesional",
    "premier division", "premiership", "super liga", "superliga",
    "brasileirao serie a", "campeonato brasileiro serie a", "liga mx",
    "major league soccer", "mls", "eredivisie", "eliteserien", "allsvenskan",
    "superettan", "j1 league", "k league 1", "a league", "pro league",
)

DEVELOPMENT_TERMS = (
    "u17", "u18", "u19", "u20", "u21", "u22", "u23",
    "under 17", "under 18", "under 19", "under 20", "under 21", "under 22", "under 23",
    "youth", "juvenil", "reserve", "reserves", "development league", "academy",
    "segunda b", "segunda division b", "serie d", "tercera division", "third division",
    "fourth division", "regional league", "amateur", "semi professional", "semiprofessional",
)

WOMEN_TERMS = (
    "women", "womens", "women s", "female", "femenil", "femenino", "femenina", "femminile",
    "frauen", "dames", "ladies", "w league", "nwsl", "liga f", "superliga femenina",
)

LOWER_LEAGUE_TERMS = (
    "serie c", "serie c group", "serie c girone", "serie d",
    "league two", "liga 3", "liga iii", "third league", "3 liga", "3rd liga",
    "segunda federacion", "tercera federacion", "national league north", "national league south",
    "primera b metropolitana", "primera c", "primera d",
    "usl league one", "usl1",
)

# Explicit allowlist for professional lower tiers with comparatively strong
# market/data coverage. Country matching prevents generic names such as
# "Serie B" or "Segunda Division" from whitelisting unrelated competitions.
TRUSTED_LOWER_TIER_BY_COUNTRY = {
    "england": (
        "championship", "efl championship", "league one", "efl league one",
    ),
    "germany": (
        "2 bundesliga", "2nd bundesliga", "zweite bundesliga",
    ),
    "spain": (
        "segunda division", "laliga 2", "la liga 2", "laliga hypermotion", "la liga hypermotion",
    ),
    "italy": (
        "serie b", "serie bkt",
    ),
    "france": (
        "ligue 2",
    ),
    "portugal": (
        "liga portugal 2", "liga portugal 2 meu super", "segunda liga",
    ),
    "brazil": (
        "brasileirao serie b", "campeonato brasileiro serie b", "serie b betano",
    ),
    "netherlands": (
        "eerste divisie",
    ),
    "belgium": (
        "challenger pro league", "first division b",
    ),
    "scotland": (
        "scottish championship", "championship",
    ),
    "sweden": (
        "superettan",
    ),
}


def _normalize(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    normalized_terms = tuple(_normalize(term) for term in terms)
    return any(term and term in text for term in normalized_terms)


def _is_trusted_lower_tier(country: str | None, competition_text: str) -> bool:
    normalized_country = _normalize(country)
    normalized_competition = _normalize(competition_text)
    terms = TRUSTED_LOWER_TIER_BY_COUNTRY.get(normalized_country, ())
    return _contains_any(normalized_competition, terms)


def _looks_like_reserve_team(name: str | None) -> bool:
    normalized = _normalize(name)
    if not normalized:
        return False
    tokens = normalized.split()
    if not tokens:
        return False
    last = tokens[-1]
    if last in {"ii", "2", "b", "res", "reserve", "reserves"}:
        return True
    if re.search(r"\b(second team|segunda plantilla|equipo b|team b)\b", normalized):
        return True
    return False


def _looks_like_women_team(name: str | None) -> bool:
    normalized = _normalize(name)
    if not normalized:
        return False
    if _contains_any(normalized, WOMEN_TERMS):
        return True
    tokens = normalized.split()
    return bool(tokens and tokens[-1] == "w")


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

    # Team identity remains a hard veto. A whitelist can never rescue women's,
    # reserve, academy or second-team fixtures.
    if _looks_like_women_team(home_name) or _looks_like_women_team(away_name):
        return CompetitionQuality(4, "TIER_4_EXCLUDED", 0.0, True, "women_team_identity")

    if _looks_like_reserve_team(home_name) or _looks_like_reserve_team(away_name):
        return CompetitionQuality(4, "TIER_4_EXCLUDED", 0.0, True, "reserve_or_second_team")

    competition_identity = _normalize(f"{fixture_name} {ref_name}")
    identity_text = _normalize(f"{fixture_name} {ref_name} {home_name} {away_name}")
    full_text = _normalize(
        f"{fixture_name} {ref_name} {competition_type} {country} {round_name} {external_id} {home_name} {away_name}"
    )

    # Hard-risk categories always win over the whitelist.
    if _contains_any(full_text, FRIENDLY_TERMS) or _normalize(competition_type) in {"friendly", "friendlies", "exhibition"}:
        return CompetitionQuality(4, "TIER_4_EXCLUDED", 0.0, True, "friendly_or_exhibition")

    if _contains_any(full_text, WOMEN_TERMS):
        return CompetitionQuality(4, "TIER_4_EXCLUDED", 0.0, True, "women_competition")

    # Trusted professional lower tiers are evaluated before generic lower-tier
    # name filters, but only after identity/friendly/women hard vetoes.
    if _is_trusted_lower_tier(country, competition_identity):
        return CompetitionQuality(2, "TIER_2_TRUSTED_LOWER", 82.0, False, "trusted_professional_lower_tier")

    if _contains_any(full_text, DEVELOPMENT_TERMS):
        return CompetitionQuality(4, "TIER_4_EXCLUDED", 0.0, True, "youth_reserve_amateur_or_development")

    if _contains_any(full_text, LOWER_LEAGUE_TERMS):
        return CompetitionQuality(4, "TIER_4_EXCLUDED", 0.0, True, "lower_league_liquidity_filter")

    identity_has_exclusion = (
        _contains_any(identity_text, FRIENDLY_TERMS)
        or _contains_any(identity_text, WOMEN_TERMS)
        or _contains_any(identity_text, DEVELOPMENT_TERMS)
        or _contains_any(identity_text, LOWER_LEAGUE_TERMS)
    )
    official_senior_identity = _contains_any(identity_text, OFFICIAL_SENIOR_TERMS) and not identity_has_exclusion

    if official_senior_identity:
        if _contains_any(identity_text, TIER1_TERMS):
            return CompetitionQuality(1, "TIER_1_ELITE", 100.0, False, "elite_official_competition")
        return CompetitionQuality(2, "TIER_2_OFFICIAL", 90.0, False, "verified_senior_official_competition")

    if _contains_any(full_text, TIER1_TERMS):
        return CompetitionQuality(1, "TIER_1_ELITE", 100.0, False, "elite_official_competition")

    return CompetitionQuality(2, "TIER_2_OFFICIAL", 85.0, False, "official_standard_competition")
