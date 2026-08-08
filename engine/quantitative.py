from __future__ import annotations

from dataclasses import dataclass, field
from math import exp, factorial
from typing import Dict, Optional


MODEL_VERSION = "v7.2-sprint2"


@dataclass(frozen=True)
class TeamProfile:
    """Pre-match team metrics already normalized to the match venue context."""

    goals_for: float
    goals_against: float
    xg_for: float
    xg_against: float
    over25_rate: float
    btts_rate: float
    clean_sheet_rate: float
    failed_to_score_rate: float
    sample_size: int = 5


@dataclass(frozen=True)
class MatchContext:
    home: TeamProfile
    away: TeamProfile
    league_avg_home_goals: float = 1.45
    league_avg_away_goals: float = 1.20
    round_number: Optional[int] = None
    home_over25_last5_home: Optional[float] = None
    away_over25_last5_away: Optional[float] = None
    home_btts_last5_home: Optional[float] = None
    away_btts_last5_away: Optional[float] = None
    recent_h2h_under25_rate: Optional[float] = None
    recent_h2h_no_btts_rate: Optional[float] = None
    tactical_pace_score: float = 0.50
    home_europe_congestion: bool = False
    away_europe_congestion: bool = False
    tournament_draw_incentive: bool = False
    lineup_attack_factor_home: float = 1.0
    lineup_attack_factor_away: float = 1.0


@dataclass(frozen=True)
class MarketQuote:
    decimal_odds: float
    bookmaker: str = ""


@dataclass
class MarketEvaluation:
    market: str
    selection: str
    probability: float
    fair_odds: float
    market_odds: Optional[float]
    implied_probability: Optional[float]
    edge: Optional[float]
    expected_value: Optional[float]
    score: float
    tier: str
    reasons: Dict[str, float | str | bool] = field(default_factory=dict)


def clamp(value: float, low: float = 0.01, high: float = 0.99) -> float:
    return max(low, min(high, value))


def poisson_probability(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return exp(-lam) * (lam**k) / factorial(k)


def probability_over_25(total_lambda: float) -> float:
    under_or_equal_two = sum(poisson_probability(k, total_lambda) for k in range(3))
    return clamp(1.0 - under_or_equal_two)


def probability_btts(home_lambda: float, away_lambda: float) -> float:
    home_scores = 1.0 - exp(-max(home_lambda, 0.0))
    away_scores = 1.0 - exp(-max(away_lambda, 0.0))
    return clamp(home_scores * away_scores)


def fair_odds(probability: float) -> float:
    return 1.0 / clamp(probability)


def implied_probability(decimal_odds: float) -> float:
    if decimal_odds <= 1.0:
        raise ValueError("Decimal odds must be greater than 1.0")
    return 1.0 / decimal_odds


def expected_value(probability: float, decimal_odds: float) -> float:
    return probability * decimal_odds - 1.0
