"""Fail-closed Over 1.5 Premium gate after FC Tokyo–Kashiwa 0-0 (2026-10-21).

This module is deliberately independent of any external probability provider.
Missing or stale evidence is a veto, not a default pass.
"""
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass(frozen=True)
class TeamEvidence:
    overall_over15_last5: Optional[int] = None
    venue_over15_last5: Optional[int] = None
    venue_over25_last5: Optional[int] = None
    scored_last5: Optional[int] = None
    scored_last10: Optional[int] = None
    overall_goals_for_avg: Optional[float] = None
    venue_goals_for_avg: Optional[float] = None
    overall_total_goals_avg: Optional[float] = None
    venue_total_goals_avg: Optional[float] = None
    failed_to_score_last10: Optional[int] = None


@dataclass(frozen=True)
class MatchEvidence:
    home: TeamEvidence
    away: TeamEvidence
    lambda_home: Optional[float] = None
    lambda_away: Optional[float] = None
    p_over15: Optional[float] = None
    p_btts: Optional[float] = None
    latest_h2h_total_goals: Optional[int] = None
    h2h_under15_last5: Optional[int] = None
    consecutive_h2h_one_team_fts: Optional[bool] = None
    data_as_of: Optional[date] = None


@dataclass(frozen=True)
class GateResult:
    approved: bool
    tier: str
    reasons: tuple[str, ...] = field(default_factory=tuple)


def evaluate_over15_premium(e: MatchEvidence, match_date: date) -> GateResult:
    reasons = []
    def require(condition: bool, message: str) -> None:
        if not condition:
            reasons.append(message)

    require(e.data_as_of is not None and 0 <= (match_date - e.data_as_of).days <= 3,
            "Datos ausentes, posteriores al partido o con antigüedad >3 días")
    for name, value in (("lambda_home", e.lambda_home), ("lambda_away", e.lambda_away),
                        ("p_over15", e.p_over15), ("p_btts", e.p_btts)):
        require(value is not None, f"Falta {name}")
    if e.lambda_home is not None and e.lambda_away is not None:
        require(e.lambda_home + e.lambda_away >= 3.05, "lambda total <3.05")
        require(min(e.lambda_home, e.lambda_away) >= 1.15, "lambda del equipo débil <1.15")
    if e.p_over15 is not None:
        require(.81 <= e.p_over15 <= 1, "P(Over1.5) <81% o inválida")
    if e.p_btts is not None:
        require(.50 <= e.p_btts <= 1, "BTTS <50% o inválida")
    for side, t in (("local", e.home), ("visitante", e.away)):
        checks = (
            (t.overall_over15_last5 == 5, "Over1.5 general no es 5/5"),
            (t.venue_over15_last5 == 5, "Over1.5 localía no es 5/5"),
            (t.venue_over25_last5 is not None and t.venue_over25_last5 >= 3,
             "Over2.5 localía <3/5 o faltante"),
            (t.scored_last5 is not None and t.scored_last5 >= 4, "Marcó <4/5 o faltante"),
            (t.scored_last10 is not None and t.scored_last10 >= 8, "Marcó <8/10 o faltante"),
            (t.overall_goals_for_avg is not None and t.overall_goals_for_avg >= 1.20,
             "GF general <1.20 o faltante"),
            (t.venue_goals_for_avg is not None and t.venue_goals_for_avg >= 1.25,
             "GF localía <1.25 o faltante"),
            (t.overall_total_goals_avg is not None and t.overall_total_goals_avg >= 2.7,
             "Goles totales general <2.7 o faltante"),
            (t.venue_total_goals_avg is not None and t.venue_total_goals_avg >= 2.8,
             "Goles totales localía <2.8 o faltante"),
            (t.failed_to_score_last10 is not None and t.failed_to_score_last10 <= 2,
             "FTS >2/10 o faltante"),
        )
        for ok, message in checks:
            require(ok, f"{side}: {message}")
    if e.home.venue_over25_last5 is not None and e.away.venue_over25_last5 is not None:
        require(max(e.home.venue_over25_last5, e.away.venue_over25_last5) >= 4,
                "Ningún equipo tiene Over2.5 localía >=4/5")
    require(e.latest_h2h_total_goals is not None and e.latest_h2h_total_goals >= 2,
            "Último H2H <=1 gol o faltante")
    require(e.h2h_under15_last5 is not None and e.h2h_under15_last5 < 2,
            "H2H <=1 gol en >=2/5 o faltante")
    require(e.consecutive_h2h_one_team_fts is False,
            "Dos H2H consecutivos con equipo sin marcar o dato faltante")
    if reasons:
        return GateResult(False, "NO_BET", tuple(reasons))
    tier = ("A" if e.lambda_home + e.lambda_away >= 3.35
            and min(e.lambda_home, e.lambda_away) >= 1.30 and e.p_over15 >= .85 else "B")
    return GateResult(True, tier)


# Incident: 2026-10-21 FC Tokyo 0–0 Kashiwa.
# Former external P(Over1.5)=91.5% was NOT validated against mandatory venue/H2H data.
# Correct classification with unavailable inputs: NO_BET, not Premium/Top 3.
