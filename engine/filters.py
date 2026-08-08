from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

from .quantitative import MatchContext, clamp


@dataclass(frozen=True)
class FilterResult:
    home_lambda_factor: float = 1.0
    away_lambda_factor: float = 1.0
    over_probability_delta: float = 0.0
    btts_probability_delta: float = 0.0
    score_delta: float = 0.0
    flags: Dict[str, float | str | bool] | None = None


def ahpc_filter(context: MatchContext) -> FilterResult:
    """Away/Home Profile Consistency.

    Penalizes a market when the local-at-home or visitor-away sample contradicts
    the aggregate signal. The logic deliberately uses only pre-match rates.
    """
    flags: Dict[str, float | str | bool] = {}
    over_delta = 0.0
    btts_delta = 0.0
    score_delta = 0.0

    if context.home_over25_last5_home is not None:
        flags["home_over25_last5_home"] = context.home_over25_last5_home
        if context.home_over25_last5_home <= 0.20:
            over_delta -= 0.04
            score_delta -= 3.0
        elif context.home_over25_last5_home >= 0.80:
            over_delta += 0.02
            score_delta += 1.0

    if context.away_over25_last5_away is not None:
        flags["away_over25_last5_away"] = context.away_over25_last5_away
        if context.away_over25_last5_away <= 0.20:
            over_delta -= 0.05
            score_delta -= 4.0
        elif context.away_over25_last5_away >= 0.80:
            over_delta += 0.02
            score_delta += 1.0

    if context.home_btts_last5_home is not None:
        flags["home_btts_last5_home"] = context.home_btts_last5_home
        if context.home_btts_last5_home <= 0.20:
            btts_delta -= 0.04
            score_delta -= 2.0

    if context.away_btts_last5_away is not None:
        flags["away_btts_last5_away"] = context.away_btts_last5_away
        if context.away_btts_last5_away <= 0.20:
            btts_delta -= 0.05
            score_delta -= 3.0

    return FilterResult(
        over_probability_delta=over_delta,
        btts_probability_delta=btts_delta,
        score_delta=score_delta,
        flags=flags,
    )


def clean_sheet_risk_filter(context: MatchContext) -> FilterResult:
    flags: Dict[str, float | str | bool] = {}
    btts_delta = 0.0
    score_delta = 0.0

    home_risk = context.home.clean_sheet_rate >= 0.35 and context.away.failed_to_score_rate >= 0.30
    away_risk = context.away.clean_sheet_rate >= 0.35 and context.home.failed_to_score_rate >= 0.30

    if home_risk:
        flags["home_clean_sheet_risk"] = True
        btts_delta -= 0.06
        score_delta -= 4.0
    if away_risk:
        flags["away_clean_sheet_risk"] = True
        btts_delta -= 0.06
        score_delta -= 4.0

    return FilterResult(btts_probability_delta=btts_delta, score_delta=score_delta, flags=flags)


def matchup_suppression_filter(context: MatchContext) -> FilterResult:
    flags: Dict[str, float | str | bool] = {}
    over_delta = 0.0
    btts_delta = 0.0
    score_delta = 0.0

    if context.recent_h2h_under25_rate is not None:
        flags["recent_h2h_under25_rate"] = context.recent_h2h_under25_rate
    if context.recent_h2h_no_btts_rate is not None:
        flags["recent_h2h_no_btts_rate"] = context.recent_h2h_no_btts_rate

    suppressed = (
        context.recent_h2h_under25_rate is not None
        and context.recent_h2h_no_btts_rate is not None
        and context.recent_h2h_under25_rate >= 0.60
        and context.recent_h2h_no_btts_rate >= 0.60
    )
    if suppressed:
        flags["matchup_suppression"] = True
        over_delta -= 0.05
        btts_delta -= 0.05
        score_delta -= 4.0

    return FilterResult(
        over_probability_delta=over_delta,
        btts_probability_delta=btts_delta,
        score_delta=score_delta,
        flags=flags,
    )


def early_season_filter(context: MatchContext) -> FilterResult:
    if context.round_number is None:
        return FilterResult(flags={})

    round_penalties: Dict[int, Tuple[float, float]] = {
        1: (0.04, 4.0),
        2: (0.03, 3.0),
        3: (0.02, 2.0),
        4: (0.01, 1.0),
        5: (0.005, 0.5),
    }
    prob_penalty, score_penalty = round_penalties.get(context.round_number, (0.0, 0.0))
    return FilterResult(
        over_probability_delta=-prob_penalty,
        btts_probability_delta=-prob_penalty,
        score_delta=-score_penalty,
        flags={"round_number": context.round_number, "early_season_penalty": prob_penalty},
    )


def europe_rotation_filter(context: MatchContext) -> FilterResult:
    count = int(context.home_europe_congestion) + int(context.away_europe_congestion)
    if count == 0:
        return FilterResult(flags={})

    penalty = 0.025 * count
    score_penalty = 3.0 * count
    return FilterResult(
        over_probability_delta=-penalty,
        btts_probability_delta=-penalty,
        score_delta=-score_penalty,
        flags={"europe_congestion_teams": count},
    )


def tactical_pace_filter(context: MatchContext) -> FilterResult:
    pace = clamp(context.tactical_pace_score, 0.0, 1.0)
    if pace < 0.40:
        return FilterResult(
            over_probability_delta=-0.04,
            btts_probability_delta=-0.02,
            score_delta=-3.0,
            flags={"tactical_pace_score": pace, "low_pace": True},
        )
    if pace > 0.70:
        return FilterResult(
            over_probability_delta=0.02,
            btts_probability_delta=0.01,
            score_delta=1.5,
            flags={"tactical_pace_score": pace, "high_pace": True},
        )
    return FilterResult(flags={"tactical_pace_score": pace})


def tournament_incentive_filter(context: MatchContext) -> FilterResult:
    if not context.tournament_draw_incentive:
        return FilterResult(flags={})
    return FilterResult(
        over_probability_delta=-0.025,
        btts_probability_delta=-0.02,
        score_delta=-2.0,
        flags={"tournament_draw_incentive": True},
    )


def lineup_filter(context: MatchContext) -> FilterResult:
    home = clamp(context.lineup_attack_factor_home, 0.70, 1.10)
    away = clamp(context.lineup_attack_factor_away, 0.70, 1.10)
    score_delta = ((home - 1.0) + (away - 1.0)) * 20.0
    return FilterResult(
        home_lambda_factor=home,
        away_lambda_factor=away,
        score_delta=score_delta,
        flags={"lineup_attack_factor_home": home, "lineup_attack_factor_away": away},
    )


def apply_filters(context: MatchContext):
    filters = [
        lineup_filter,
        ahpc_filter,
        clean_sheet_risk_filter,
        matchup_suppression_filter,
        early_season_filter,
        europe_rotation_filter,
        tactical_pace_filter,
        tournament_incentive_filter,
    ]

    home_factor = 1.0
    away_factor = 1.0
    over_delta = 0.0
    btts_delta = 0.0
    score_delta = 0.0
    reasons: Dict[str, float | str | bool] = {}

    for filter_fn in filters:
        result = filter_fn(context)
        home_factor *= result.home_lambda_factor
        away_factor *= result.away_lambda_factor
        over_delta += result.over_probability_delta
        btts_delta += result.btts_probability_delta
        score_delta += result.score_delta
        if result.flags:
            reasons.update(result.flags)

    return home_factor, away_factor, over_delta, btts_delta, score_delta, reasons
