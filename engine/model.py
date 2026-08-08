from __future__ import annotations

from typing import Dict, Optional

from .filters import apply_filters
from .quantitative import (
    MODEL_VERSION,
    MarketEvaluation,
    MarketQuote,
    MatchContext,
    clamp,
    expected_value,
    fair_odds,
    implied_probability,
    probability_btts,
    probability_over_25,
)


class FootballValueEngine:
    """Transparent rule-based quantitative engine for BTTS and Over 2.5.

    Every adjustment is exposed in `reasons` so future backtests can measure
    whether each filter improves calibration and ROI.
    """

    min_edge = 0.06
    min_ev = 0.08
    min_btts_probability = 0.63
    min_over25_probability = 0.65
    min_score = 80.0

    def estimate_base_lambdas(self, context: MatchContext) -> tuple[float, float, Dict[str, float]]:
        # Blend actual scoring, xG, opponent defensive output and league baseline.
        # Venue-specific profiles should be supplied by the scanner.
        home_attack = 0.45 * context.home.goals_for + 0.35 * context.home.xg_for + 0.20 * context.league_avg_home_goals
        away_defence = 0.45 * context.away.goals_against + 0.35 * context.away.xg_against + 0.20 * context.league_avg_home_goals
        away_attack = 0.45 * context.away.goals_for + 0.35 * context.away.xg_for + 0.20 * context.league_avg_away_goals
        home_defence = 0.45 * context.home.goals_against + 0.35 * context.home.xg_against + 0.20 * context.league_avg_away_goals

        home_lambda = max(0.20, (home_attack + away_defence) / 2.0)
        away_lambda = max(0.20, (away_attack + home_defence) / 2.0)

        return home_lambda, away_lambda, {
            "base_home_lambda": round(home_lambda, 4),
            "base_away_lambda": round(away_lambda, 4),
        }

    def _score(self, probability: float, context_score_delta: float, edge: Optional[float], ev: Optional[float]) -> float:
        # 60 points represent the football profile, scaled around the useful
        # 50%-75% probability range. Edge and EV contribute up to 30 points.
        probability_component = 50.0 + clamp((probability - 0.50) / 0.25, 0.0, 1.0) * 30.0
        value_component = 0.0
        if edge is not None:
            value_component += clamp(edge / 0.12, 0.0, 1.0) * 15.0
        if ev is not None:
            value_component += clamp(ev / 0.20, 0.0, 1.0) * 15.0
        score = probability_component + value_component + context_score_delta
        return round(clamp(score, 0.0, 100.0), 2)

    def _tier(self, market: str, probability: float, score: float, edge: Optional[float], ev: Optional[float]) -> str:
        probability_floor = self.min_btts_probability if market == "BTTS" else self.min_over25_probability
        if edge is None or ev is None:
            return "CANDIDATE" if probability >= probability_floor and score >= self.min_score else ""
        if probability >= probability_floor and edge >= self.min_edge and ev >= self.min_ev and score >= self.min_score:
            return "TIER_A"
        return ""

    def evaluate(self, context: MatchContext, btts_quote: Optional[MarketQuote] = None, over25_quote: Optional[MarketQuote] = None):
        home_lambda, away_lambda, base_reasons = self.estimate_base_lambdas(context)
        home_factor, away_factor, over_delta, btts_delta, score_delta, filter_reasons = apply_filters(context)

        home_lambda *= home_factor
        away_lambda *= away_factor
        total_lambda = home_lambda + away_lambda

        base_btts = probability_btts(home_lambda, away_lambda)
        base_over = probability_over_25(total_lambda)

        # A small structural blend prevents pure Poisson from ignoring repeated
        # venue-specific BTTS/Over behaviour while still keeping Poisson central.
        btts_structural = (context.home.btts_rate + context.away.btts_rate) / 2.0
        over_structural = (context.home.over25_rate + context.away.over25_rate) / 2.0

        btts_probability = clamp(0.80 * base_btts + 0.20 * btts_structural + btts_delta)
        over_probability = clamp(0.80 * base_over + 0.20 * over_structural + over_delta)

        reasons = {
            **base_reasons,
            **filter_reasons,
            "adjusted_home_lambda": round(home_lambda, 4),
            "adjusted_away_lambda": round(away_lambda, 4),
            "total_lambda": round(total_lambda, 4),
            "base_btts_poisson": round(base_btts, 5),
            "base_over25_poisson": round(base_over, 5),
            "structural_btts_rate": round(btts_structural, 5),
            "structural_over25_rate": round(over_structural, 5),
            "context_score_delta": round(score_delta, 2),
            "model_version": MODEL_VERSION,
        }

        return {
            "btts": self._market_evaluation("BTTS", "YES", btts_probability, btts_quote, score_delta, reasons),
            "over25": self._market_evaluation("OVER_2_5", "OVER", over_probability, over25_quote, score_delta, reasons),
        }

    def _market_evaluation(
        self,
        market: str,
        selection: str,
        probability: float,
        quote: Optional[MarketQuote],
        score_delta: float,
        reasons: Dict[str, float | str | bool],
    ) -> MarketEvaluation:
        odds = quote.decimal_odds if quote else None
        implied = implied_probability(odds) if odds else None
        edge = probability - implied if implied is not None else None
        ev = expected_value(probability, odds) if odds is not None else None
        score = self._score(probability, score_delta, edge, ev)
        tier = self._tier("BTTS" if market == "BTTS" else "OVER_2_5", probability, score, edge, ev)

        market_reasons = dict(reasons)
        if quote:
            market_reasons.update({
                "bookmaker": quote.bookmaker,
                "market_odds": odds,
                "implied_probability": round(implied, 5),
                "edge": round(edge, 5),
                "expected_value": round(ev, 5),
            })

        return MarketEvaluation(
            market=market,
            selection=selection,
            probability=round(probability, 5),
            fair_odds=round(fair_odds(probability), 3),
            market_odds=odds,
            implied_probability=round(implied, 5) if implied is not None else None,
            edge=round(edge, 5) if edge is not None else None,
            expected_value=round(ev, 5) if ev is not None else None,
            score=score,
            tier=tier,
            reasons=market_reasons,
        )
