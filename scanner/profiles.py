from __future__ import annotations

from dataclasses import dataclass
from statistics import mean

from engine.quantitative import MatchContext, TeamProfile


def _finished_score(item: dict) -> tuple[int, int] | None:
    goals = item.get("goals") or {}
    home = goals.get("home")
    away = goals.get("away")
    if home is None or away is None:
        return None
    return int(home), int(away)


def _team_id(item: dict, side: str) -> str:
    return str((((item.get("teams") or {}).get(side) or {}).get("id", "")))


def venue_sample(fixtures: list[dict], team_id: int | str, *, venue: str, limit: int = 5) -> list[dict]:
    wanted = str(team_id)
    side = "home" if venue == "home" else "away"
    selected: list[dict] = []
    for item in fixtures:
        if _team_id(item, side) == wanted and _finished_score(item) is not None:
            selected.append(item)
        if len(selected) >= limit:
            break
    return selected


def build_team_profile(fixtures: list[dict], team_id: int | str, *, venue: str, limit: int = 5) -> TeamProfile:
    sample = venue_sample(fixtures, team_id, venue=venue, limit=limit)
    if not sample:
        # Neutral baseline. A low sample size is explicitly penalized elsewhere.
        return TeamProfile(1.20, 1.20, 1.20, 1.20, 0.50, 0.50, 0.20, 0.20, sample_size=0)

    goals_for: list[int] = []
    goals_against: list[int] = []
    overs = btts = clean_sheets = failed = 0
    for item in sample:
        home_goals, away_goals = _finished_score(item) or (0, 0)
        if venue == "home":
            gf, ga = home_goals, away_goals
        else:
            gf, ga = away_goals, home_goals
        goals_for.append(gf)
        goals_against.append(ga)
        overs += int(gf + ga >= 3)
        btts += int(gf > 0 and ga > 0)
        clean_sheets += int(ga == 0)
        failed += int(gf == 0)

    n = len(sample)
    gf_avg = mean(goals_for)
    ga_avg = mean(goals_against)
    return TeamProfile(
        goals_for=gf_avg,
        goals_against=ga_avg,
        # Until event-level xG is connected, observed goals act as a transparent proxy.
        xg_for=gf_avg,
        xg_against=ga_avg,
        over25_rate=overs / n,
        btts_rate=btts / n,
        clean_sheet_rate=clean_sheets / n,
        failed_to_score_rate=failed / n,
        sample_size=n,
    )


def h2h_suppression(h2h: list[dict]) -> tuple[float | None, float | None]:
    scored = [_finished_score(item) for item in h2h]
    scored = [score for score in scored if score is not None]
    if not scored:
        return None, None
    under = sum(1 for h, a in scored if h + a <= 2) / len(scored)
    no_btts = sum(1 for h, a in scored if h == 0 or a == 0) / len(scored)
    return under, no_btts


def parse_round_number(fixture: dict) -> int | None:
    round_text = str(((fixture.get("league") or {}).get("round") or ""))
    digits = "".join(ch for ch in round_text if ch.isdigit())
    return int(digits) if digits else None


def build_match_context(
    fixture: dict,
    home_history: list[dict],
    away_history: list[dict],
    h2h: list[dict],
    *,
    home_europe_congestion: bool = False,
    away_europe_congestion: bool = False,
    tournament_draw_incentive: bool = False,
) -> MatchContext:
    teams = fixture.get("teams") or {}
    home_id = (teams.get("home") or {}).get("id")
    away_id = (teams.get("away") or {}).get("id")
    home = build_team_profile(home_history, home_id, venue="home")
    away = build_team_profile(away_history, away_id, venue="away")
    h2h_under, h2h_no_btts = h2h_suppression(h2h)

    return MatchContext(
        home=home,
        away=away,
        round_number=parse_round_number(fixture),
        home_over25_last5_home=home.over25_rate,
        away_over25_last5_away=away.over25_rate,
        home_btts_last5_home=home.btts_rate,
        away_btts_last5_away=away.btts_rate,
        recent_h2h_under25_rate=h2h_under,
        recent_h2h_no_btts_rate=h2h_no_btts,
        home_europe_congestion=home_europe_congestion,
        away_europe_congestion=away_europe_congestion,
        tournament_draw_incentive=tournament_draw_incentive,
    )
