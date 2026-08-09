from __future__ import annotations

from dataclasses import dataclass

from .features import FeatureVector


@dataclass(frozen=True)
class MarketIntelligenceResult:
    score: float
    passed: bool
    failures: list[str]
    evidence: dict[str, float | int | bool | None]


class MarketIntelligenceService:
    """Sprint 6.7 calibrated market-selection intelligence.

    Signals are primarily graduated penalties. Hard rejection is reserved for
    extreme contradictions so multiple sensible filters cannot accidentally
    collapse the whole daily card into NO BET.
    """

    min_score = 40.0
    neutral_score = 65.0

    @staticmethod
    def _weighted_rate(home_rate: float, away_rate: float, home_weight: float, away_weight: float) -> float:
        total = home_weight + away_weight
        if total <= 0:
            return 0.50
        return (home_rate * home_weight + away_rate * away_weight) / total

    @staticmethod
    def _scaled(value: float, target: float) -> float:
        if target <= 0:
            return 0.0
        return max(0.0, min(100.0, value / target * 100.0))

    def evaluate(self, features: FeatureVector, market: str) -> MarketIntelligenceResult:
        home = features.home_profile
        away = features.away_profile
        home_weight = max(1.0, float(home.sample_size))
        away_weight = max(1.0, float(away.sample_size))

        home_btts_weight = max(0.5, float(home.sample_size) * float(home.btts_rate))
        away_btts_weight = max(0.5, float(away.sample_size) * float(away.btts_rate))
        escalation = self._weighted_rate(
            float(home.btts_over25_escalation_rate),
            float(away.btts_over25_escalation_rate),
            home_btts_weight,
            away_btts_weight,
        )
        low_score_rate = self._weighted_rate(
            float(home.low_score_rate),
            float(away.low_score_rate),
            home_weight,
            away_weight,
        )
        one_one_rate = self._weighted_rate(
            float(home.one_one_rate),
            float(away.one_one_rate),
            home_weight,
            away_weight,
        )
        btts_rate = self._weighted_rate(
            float(home.btts_rate),
            float(away.btts_rate),
            home_weight,
            away_weight,
        )
        over_rate = self._weighted_rate(
            float(home.over25_rate),
            float(away.over25_rate),
            home_weight,
            away_weight,
        )
        avg_total_goals = self._weighted_rate(
            float(home.goals_for + home.goals_against),
            float(away.goals_for + away.goals_against),
            home_weight,
            away_weight,
        )
        failed_to_score = self._weighted_rate(
            float(home.failed_to_score_rate),
            float(away.failed_to_score_rate),
            home_weight,
            away_weight,
        )
        clean_sheet = self._weighted_rate(
            float(home.clean_sheet_rate),
            float(away.clean_sheet_rate),
            home_weight,
            away_weight,
        )

        failures: list[str] = []
        enough_sample = home.sample_size >= 3 and away.sample_size >= 3
        hard_reject = False

        if market == "OVER_2_5":
            escalation_component = self._scaled(escalation, 0.70)
            volatility_component = max(0.0, min(100.0, (1.0 - low_score_rate) * 100.0 / 0.55))
            goals_component = self._scaled(avg_total_goals, 3.0)
            score = 0.45 * escalation_component + 0.35 * volatility_component + 0.20 * goals_component

            # Sprint 6.7: prefer-BTTS is evidence, not an automatic veto. Only an
            # extreme 1-1/low-escalation profile may trigger a hard rejection.
            prefer_btts = (
                enough_sample
                and features.btts_market_odds is not None
                and btts_rate >= 0.58
                and escalation <= 0.50
            )
            if prefer_btts:
                score -= 10
                failures.append("prefer_btts_over_over25")

            if enough_sample and low_score_rate >= 0.70:
                score -= 14
                failures.append("low_score_script_very_high")
            elif enough_sample and low_score_rate >= 0.60:
                score -= 9
                failures.append("low_score_script_high")
            elif enough_sample and low_score_rate >= 0.50:
                score -= 5
                failures.append("low_score_script_elevated")

            if enough_sample and avg_total_goals < 2.10:
                score -= 12
                failures.append("avg_total_goals_very_low")
            elif enough_sample and avg_total_goals < 2.30:
                score -= 8
                failures.append("avg_total_goals_low")
            elif enough_sample and avg_total_goals < 2.50:
                score -= 4
                failures.append("avg_total_goals_soft")

            if enough_sample and one_one_rate >= 0.40:
                score -= 7
                failures.append("one_one_pattern_very_high")
            elif enough_sample and one_one_rate >= 0.30:
                score -= 4
                failures.append("one_one_pattern_high")

            extreme_btts_mismatch = (
                enough_sample
                and btts_rate >= 0.65
                and escalation <= 0.30
                and low_score_rate >= 0.55
            )
            extreme_low_score_script = (
                enough_sample
                and low_score_rate >= 0.72
                and avg_total_goals < 2.15
            )
            if extreme_btts_mismatch:
                failures.append("extreme_btts_over_mismatch")
            if extreme_low_score_script:
                failures.append("extreme_low_score_script")
            hard_reject = extreme_btts_mismatch or extreme_low_score_script

        elif market == "BTTS":
            btts_component = self._scaled(btts_rate, 0.65)
            scoring_reliability = max(0.0, min(100.0, (1.0 - failed_to_score) * 100.0 / 0.85))
            mutual_concession = max(0.0, min(100.0, (1.0 - clean_sheet) * 100.0 / 0.85))
            score = 0.50 * btts_component + 0.30 * scoring_reliability + 0.20 * mutual_concession

            if enough_sample and btts_rate < 0.35:
                score -= 14
                failures.append("combined_btts_rate_very_low")
            elif enough_sample and btts_rate < 0.45:
                score -= 8
                failures.append("combined_btts_rate_low")
            if enough_sample and failed_to_score >= 0.50:
                score -= 10
                failures.append("failed_to_score_risk_high")
            elif enough_sample and failed_to_score >= 0.40:
                score -= 6
                failures.append("failed_to_score_risk")
            if enough_sample and clean_sheet >= 0.50:
                score -= 8
                failures.append("clean_sheet_risk_high")
            elif enough_sample and clean_sheet >= 0.40:
                score -= 5
                failures.append("clean_sheet_risk")

            hard_reject = (
                enough_sample
                and btts_rate < 0.30
                and (failed_to_score >= 0.50 or clean_sheet >= 0.55)
            )
            if hard_reject:
                failures.append("extreme_btts_contradiction")
        else:
            return MarketIntelligenceResult(100.0, True, [], {})

        score = round(max(0.0, min(100.0, score)), 1)
        passed = score >= self.min_score and not hard_reject
        if score < self.min_score:
            failures.append("market_intelligence_below_40")

        evidence = {
            "goal_escalation_index": round(escalation, 3),
            "combined_low_score_rate": round(low_score_rate, 3),
            "combined_one_one_rate": round(one_one_rate, 3),
            "combined_btts_rate": round(btts_rate, 3),
            "combined_over25_rate": round(over_rate, 3),
            "combined_avg_total_goals": round(avg_total_goals, 3),
            "combined_failed_to_score_rate": round(failed_to_score, 3),
            "combined_clean_sheet_rate": round(clean_sheet, 3),
            "home_sample": int(home.sample_size),
            "away_sample": int(away.sample_size),
            "enough_sample": enough_sample,
            "hard_reject": hard_reject,
            "btts_quote_available": features.btts_market_odds is not None,
            "over25_quote_available": features.over25_market_odds is not None,
        }
        return MarketIntelligenceResult(score, passed, failures, evidence)
