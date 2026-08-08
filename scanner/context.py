from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from engine.quantitative import MatchContext
from .providers.base import SportsDataProvider


EUROPE_KEYWORDS = (
    "UEFA Champions League",
    "UEFA Europa League",
    "UEFA Conference League",
    "Champions League",
    "Europa League",
    "Conference League",
)

DRAW_INCENTIVE_COMPETITIONS = (
    "Leagues Cup",
)


def _fixture_id(raw: dict) -> str:
    return str(((raw.get("fixture") or {}).get("id") or ""))


def _fixture_datetime(raw: dict) -> datetime | None:
    value = (raw.get("fixture") or {}).get("date")
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _league_name(raw: dict) -> str:
    return str((raw.get("league") or {}).get("name") or "")


def parse_round_number(raw_fixture: dict) -> int | None:
    round_text = str((raw_fixture.get("league") or {}).get("round") or "")
    numbers = re.findall(r"\d+", round_text)
    return int(numbers[-1]) if numbers else None


def _is_europe_fixture(raw: dict) -> bool:
    name = _league_name(raw).lower()
    return any(keyword.lower() in name for keyword in EUROPE_KEYWORDS)


def _europe_window(
    provider: SportsDataProvider,
    team_id: int | str,
    raw_fixture: dict,
    days: int = 4,
) -> tuple[bool, dict[str, Any]]:
    kickoff = _fixture_datetime(raw_fixture)
    if kickoff is None:
        return False, {"europe_context_available": False}

    current_id = _fixture_id(raw_fixture)
    start = (kickoff - timedelta(days=days)).date()
    end = (kickoff + timedelta(days=days)).date()
    fixtures = provider.team_fixtures_between(team_id, start, end)

    previous_europe: list[dict] = []
    next_europe: list[dict] = []
    for item in fixtures:
        if _fixture_id(item) == current_id or not _is_europe_fixture(item):
            continue
        dt = _fixture_datetime(item)
        if dt is None:
            continue
        entry = {
            "fixture_id": _fixture_id(item),
            "competition": _league_name(item),
            "date": dt.isoformat(),
            "days_from_match": round((dt - kickoff).total_seconds() / 86400, 2),
        }
        if dt < kickoff:
            previous_europe.append(entry)
        elif dt > kickoff:
            next_europe.append(entry)

    # ERI is strongest when a domestic game is literally sandwiched between
    # European matches. One-sided congestion remains visible in metadata but
    # does not trigger the full model penalty yet.
    sandwiched = bool(previous_europe and next_europe)
    metadata = {
        "europe_context_available": True,
        "europe_before": previous_europe,
        "europe_after": next_europe,
        "europe_sandwich": sandwiched,
    }
    return sandwiched, metadata


def _starter_ids(lineup: dict) -> set[int]:
    result: set[int] = set()
    for row in lineup.get("startXI") or []:
        player = row.get("player") or {}
        if player.get("id") is not None:
            result.add(int(player["id"]))
    return result


def _attacking_starters(lineup: dict) -> int:
    count = 0
    for row in lineup.get("startXI") or []:
        player = row.get("player") or {}
        pos = str(player.get("pos") or "").upper()
        # API-Football normally uses G/D/M/F. Midfielders matter for chance
        # creation, but forwards receive the strongest weight below.
        if pos in {"M", "F"}:
            count += 1
    return count


def _lineup_for_team(lineups: list[dict], team_id: int | str) -> dict | None:
    target = str(team_id)
    for lineup in lineups:
        if str((lineup.get("team") or {}).get("id")) == target:
            return lineup
    return None


def _lineup_factor(
    provider: SportsDataProvider,
    team_id: int | str,
    current_fixture_id: int | str,
    recent_history: list[dict],
) -> tuple[float, dict[str, Any]]:
    current_lineup = _lineup_for_team(provider.fixture_lineups(current_fixture_id), team_id)
    if not current_lineup:
        return 1.0, {"lineup_available": False, "lineup_status": "pending"}

    previous_fixture_id = None
    for item in recent_history:
        candidate = (item.get("fixture") or {}).get("id")
        if candidate and str(candidate) != str(current_fixture_id):
            previous_fixture_id = candidate
            break

    previous_lineup = None
    if previous_fixture_id:
        previous_lineup = _lineup_for_team(provider.fixture_lineups(previous_fixture_id), team_id)

    current_ids = _starter_ids(current_lineup)
    current_attackers = _attacking_starters(current_lineup)
    factor = 1.0
    metadata: dict[str, Any] = {
        "lineup_available": True,
        "current_starting_xi_size": len(current_ids),
        "current_attacking_starters": current_attackers,
        "previous_lineup_available": bool(previous_lineup),
    }

    if previous_lineup:
        previous_ids = _starter_ids(previous_lineup)
        previous_attackers = _attacking_starters(previous_lineup)
        overlap = len(current_ids & previous_ids)
        continuity = overlap / max(len(previous_ids), 1)
        metadata.update({
            "previous_attacking_starters": previous_attackers,
            "starting_xi_overlap": overlap,
            "starting_xi_continuity": round(continuity, 3),
        })

        if continuity < 0.45:
            factor *= 0.92
        elif continuity < 0.60:
            factor *= 0.95
        elif continuity < 0.72:
            factor *= 0.98

        attacking_drop = max(0, previous_attackers - current_attackers)
        factor *= max(0.91, 1.0 - 0.025 * attacking_drop)
        metadata["attacking_starter_drop"] = attacking_drop

    metadata["lineup_attack_factor"] = round(factor, 3)
    return factor, metadata


def _venue_metadata(raw_fixture: dict) -> dict[str, Any]:
    fixture = raw_fixture.get("fixture") or {}
    venue = fixture.get("venue") or {}
    teams = raw_fixture.get("teams") or {}
    # Crucially, local status is sourced from the provider's structured `home`
    # field and the official fixture venue, never inferred from display order.
    return {
        "venue_source": "api_football_fixture",
        "venue_name": venue.get("name") or "",
        "venue_city": venue.get("city") or "",
        "official_home_team_id": (teams.get("home") or {}).get("id"),
        "official_away_team_id": (teams.get("away") or {}).get("id"),
        "venue_verified": bool(venue.get("name") or venue.get("city")),
    }


def enrich_match_context(
    provider: SportsDataProvider,
    raw_fixture: dict,
    context: MatchContext,
    home_history: list[dict],
    away_history: list[dict],
) -> tuple[MatchContext, dict[str, Any]]:
    teams = raw_fixture.get("teams") or {}
    home_id = (teams.get("home") or {}).get("id")
    away_id = (teams.get("away") or {}).get("id")
    fixture_id = (raw_fixture.get("fixture") or {}).get("id")

    round_number = parse_round_number(raw_fixture)
    home_europe, home_europe_meta = _europe_window(provider, home_id, raw_fixture)
    away_europe, away_europe_meta = _europe_window(provider, away_id, raw_fixture)
    home_lineup_factor, home_lineup_meta = _lineup_factor(provider, home_id, fixture_id, home_history)
    away_lineup_factor, away_lineup_meta = _lineup_factor(provider, away_id, fixture_id, away_history)

    competition = _league_name(raw_fixture)
    draw_incentive = any(name.lower() in competition.lower() for name in DRAW_INCENTIVE_COMPETITIONS)

    enriched = replace(
        context,
        round_number=round_number,
        home_europe_congestion=home_europe,
        away_europe_congestion=away_europe,
        tournament_draw_incentive=draw_incentive,
        lineup_attack_factor_home=home_lineup_factor,
        lineup_attack_factor_away=away_lineup_factor,
    )

    metadata: dict[str, Any] = {
        "advanced_context_version": "sprint2-context-v1",
        "round_number_detected": round_number,
        "competition_name": competition,
        "tournament_draw_incentive_detected": draw_incentive,
        **_venue_metadata(raw_fixture),
        "home_europe_context": home_europe_meta,
        "away_europe_context": away_europe_meta,
        "home_lineup_context": home_lineup_meta,
        "away_lineup_context": away_lineup_meta,
    }
    return enriched, metadata
