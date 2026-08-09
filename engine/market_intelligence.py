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
    """Sprint 6.6 market-selection intelligence using persisted venue evidence only.

    This layer does not call external APIs. It distinguishes a strong BTTS profile
    from a true high-goal profile by measuring how often BTTS games actually
    escalate to 3+ goals, and it penalizes stable low-scoring match scripts.
    """

    min_score = 45.0
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

        # Approximate the number of BTTS observations so escalation evidence is
        # weighted by how many actual BTTS matches each venue profile contains.
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

        if market == "OVER_2_5":
            escalation_component = self._scaled(escalation, 0.70)
            volatility_component = max(0.0, min(100.0, (1.0 - low_score_rate) * 100.0 / 0.55))
            goals_component = self._scaled(avg_total_goals, 3.0)
            score = 0.45 * escalation_component + 0.35 * volatility_component + 0.20 * goals_component

            # Prefer BTTS when both teams often score but their BTTS games do not
            # reliably produce a third goal. This is the Supra/Cavalry-type pattern.
            prefer_btts = (
                enough_sample
                and features.btts_market_odds is not None
                and btts_rate >= 0.58
                and escalation <= 0.50
            )
            if prefer_btts:
                score -= 18
                failures.append("prefer_btts_over_over25")

            if enough_sample and low_score_rate >= 0.60:
                score -= 14
                failures.append("low_score_script_high")
            elif enough_sample and low_score_rate >= 0.50:
                score -= 7
                failures.append("low_score_script_elevated")

            if enough_sample and avg_total_goals < 2.25:
                score -= 12
                failures.append("avg_total_goals_low")
            elif enough_sample and avg_total_goals < 2.50:
                score -= 6
                failures.append("avg_total_goals_soft")

            if enough_sample and one_one_rate >= 0.30:
                score -= 8
                failures.append("one_one_pattern_high")

            # A strong low-score script plus weak totals is a hard contradiction
            # to Over 2.5 even when the market price creates attractive EV.
            hard_script_reject = enough_sample and low_score_rate >= 0.60 and avg_total_goals < 2.35
            if hard_script_reject:
                failures.append("strong_low_score_script")

            passed = score >= self.min_score and not prefer_btts and not hard_script_reject
        elif market == "BTTS":
            btts_component = self._scaled(btts_rate, 0.65)
            scoring_reliability = max(0.0, min(100.0, (1.0 - failed_to_score) * 100.0 / 0.85))
            mutual_concession = max(0.0, min(100.0, (1.0 - clean_sheet) * 100.0 / 0.85))
            score = 0.50 * btts_component + 0.30 * scoring_reliability + 0.20 * mutual_concession

            if enough_sample and btts_rate < 0.45:
                score -= 12
                failures.append("combined_btts_rate_low")
            if enough_sample and failed_to_score >= 0.40:
                score -= 10
                failures.append("failed_to_score_risk")
            if enough_sample and clean_sheet >= 0.40:
                score -= 8
                failures.append("clean_sheet_risk")

            passed = score >= self.min_score
        else:
            return MarketIntelligenceResult(100.0, True, [], {})

        score = round(max(0.0, min(100.0, score)), 1)
        if score < self.min_score and "market_intelligence_below_45" not in failures:
            failures.append("market_intelligence_below_45")

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
            "btts_quote_available": features.btts_market_odds is not None,
            "over25_quote_available": features.over25_market_odds is not None,
        }
        return MarketIntelligenceResult(score, passed, failures, evidence)
