from __future__ import annotations

"""BTTS V2.6 precision/recall rebalance.

V2.5 reacted to a 0/3 day by turning several correlated signals into hard
vetoes. In production this could collapse a large fixture slate to zero picks.
V2.6 keeps the anti-zero fundamentals as hard gates (both teams must actually
score with adequate frequency in the exact venue role and recent overall form),
but stops treating missing H2H and a 3/5 bilateral-BTTS streak as mandatory.

The important distinction is data absence vs. negative evidence:
* missing/short H2H is neutral;
* a sufficiently large H2H sample with repeated BTTS-NO remains a veto;
* recent scoring ability remains mandatory;
* BTTS participation remains in the consensus/ranking signal, with a modest
  minimum, instead of requiring 3/5 for both teams independently.
"""

V26_MIN_OVERALL_SCORE_RATE = 0.55
V26_MIN_OVERALL_LAST5_SCORED = 3
V26_MIN_CALIBRATED_PROB = 0.56
V26_MIN_CONSENSUS_PROB = 0.53
V26_MIN_EMPIRICAL_BTTS = 0.42


def _anti_zero_decision_v26(prediction):
    from .btts_v25_policy import (
        MIN_OVERALL_SAMPLE,
        MIN_ROLE_SAMPLE,
        PREMIUM_MAX_FTS,
        PREMIUM_MAX_ZERO_RISK,
        PREMIUM_MIN_AVG_GF,
        PREMIUM_MIN_LAST5_SCORED,
        PREMIUM_MIN_MEDIAN_GF,
        PREMIUM_MIN_SCORE_RATE,
        anti_zero_metrics,
    )
    from .premium_risk_guard import PremiumRiskDecision

    metrics = anti_zero_metrics(prediction)
    if not metrics.get("available"):
        # Optional history enrichment must never turn the entire slate into NO BET.
        # Core selector/risk/deep-analysis gates are still authoritative.
        return None

    for side in ("home", "away"):
        role = metrics[side]
        if role["n"] < MIN_ROLE_SAMPLE:
            return PremiumRiskDecision(True, f"{side}_v26_sample_incomplete", f"{side} role sample n={role['n']}<5")
        if role["score_rate"] < PREMIUM_MIN_SCORE_RATE:
            return PremiumRiskDecision(True, f"{side}_v26_low_score_rate", f"{side} scores {role['score_rate']:.0%}<60%")
        if role["avg_gf"] < PREMIUM_MIN_AVG_GF:
            return PremiumRiskDecision(True, f"{side}_v26_low_avg_gf", f"{side} avgGF={role['avg_gf']:.2f}<0.90")
        if role["failed_to_score_rate"] > PREMIUM_MAX_FTS:
            return PremiumRiskDecision(True, f"{side}_v26_fts", f"{side} FTS={role['failed_to_score_rate']:.0%}>40%")
        if role["median_gf"] < PREMIUM_MIN_MEDIAN_GF:
            return PremiumRiskDecision(True, f"{side}_v26_low_median", f"{side} medianGF={role['median_gf']:.1f}<1.0")
        if role["last5_scored"] < PREMIUM_MIN_LAST5_SCORED:
            return PremiumRiskDecision(True, f"{side}_v26_recent_blanks", f"{side} scored {role['last5_scored']}/5")
        if role["zero_risk"] > PREMIUM_MAX_ZERO_RISK:
            return PremiumRiskDecision(True, f"{side}_v26_zero_risk", f"{side} zero-risk={role['zero_risk']:.1%}>35%")

        overall = metrics[f"{side}_overall"]
        if overall["n"] < MIN_OVERALL_SAMPLE:
            return PremiumRiskDecision(True, f"{side}_v26_overall_sample", f"{side} overall sample n={overall['n']}<5")
        if overall["score_rate"] < V26_MIN_OVERALL_SCORE_RATE:
            return PremiumRiskDecision(True, f"{side}_v26_overall_score_rate", f"{side} overall scores {overall['score_rate']:.0%}<55%")
        if overall["last5_scored"] < V26_MIN_OVERALL_LAST5_SCORED:
            return PremiumRiskDecision(True, f"{side}_v26_overall_recent_blanks", f"{side} overall scored {overall['last5_scored']}/5")

    if metrics["calibrated_probability"] < V26_MIN_CALIBRATED_PROB:
        return PremiumRiskDecision(
            True,
            "v26_calibrated_probability_floor",
            f"calibrated BTTS={metrics['calibrated_probability']:.1%}<56%",
        )
    if metrics["consensus_probability"] < V26_MIN_CONSENSUS_PROB:
        return PremiumRiskDecision(
            True,
            "v26_consensus_probability_floor",
            f"consensus BTTS={metrics['consensus_probability']:.1%}<53%",
        )
    if metrics["empirical_btts"] < V26_MIN_EMPIRICAL_BTTS:
        return PremiumRiskDecision(
            True,
            "v26_empirical_btts_floor",
            f"empirical BTTS={metrics['empirical_btts']:.1%}<42%",
        )
    return None


def install_btts_v26_policy() -> None:
    """Install the production V2.6 rebalance after V2.1-V2.5 are loaded."""
    from . import btts_v25_policy
    from .premium_risk_guard import PremiumRiskGuard
    from .btts_h2h_guard import h2h_metrics

    if getattr(PremiumRiskGuard, "_btts_v26_installed", False):
        return

    # 1) Rebalance V2.5 hard vetoes while retaining its anti-zero metrics.
    btts_v25_policy.anti_zero_decision = _anti_zero_decision_v26

    # 2) Missing H2H is missing data, not negative BTTS evidence. Keep the
    # original hard contradiction logic whenever at least three H2Hs exist.
    original_h2h_risk = PremiumRiskGuard._h2h_risk.__func__

    def h2h_risk_v26(
        cls,
        prediction,
        *,
        h_recent,
        a_recent,
        h_long,
        a_long,
        h_fts,
        a_fts,
    ):
        metrics = h2h_metrics(prediction)
        sample = int(metrics.get("sample") or 0)
        if sample < cls.BTTS_H2H_MIN_EVIDENCE:
            return None
        return original_h2h_risk(
            cls,
            prediction,
            h_recent=h_recent,
            a_recent=a_recent,
            h_long=h_long,
            a_long=a_long,
            h_fts=h_fts,
            a_fts=a_fts,
        )

    PremiumRiskGuard._h2h_risk = classmethod(h2h_risk_v26)
    PremiumRiskGuard._btts_v26_installed = True
