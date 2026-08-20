from __future__ import annotations

"""BTTS V2.2 publication quality floor.

V2.2 does not change the underlying BTTS probability model. It prevents weak
post-penalty candidates from being published merely because the raw prediction
score was high enough before venue/H2H/disagreement penalties.
"""

BTTS_V22_MIN_EFFECTIVE_RELIABILITY = 0.85
BTTS_V22_MIN_FINAL_RANK = 75.0


def install_btts_v22_policy() -> None:
    from .premium_selection import DailyPremiumSelector

    if getattr(DailyPremiumSelector, "_btts_v22_installed", False):
        return

    original_passes = DailyPremiumSelector._passes_hard_value_floors.__func__
    original_rank_candidates = DailyPremiumSelector._rank_candidates

    @classmethod
    def passes_v22(cls, prediction):
        if not original_passes(cls, prediction):
            return False

        calibration = cls.calibrator.calibrate(prediction)
        disagreement = cls._disagreement_metrics(prediction)
        venue = cls._venue_contradiction_metrics(prediction)
        effective_reliability = max(
            0.0,
            float(disagreement.get("effective_reliability") or 0.0)
            - float(venue.get("reliability_penalty") or 0.0),
        )
        return effective_reliability >= BTTS_V22_MIN_EFFECTIVE_RELIABILITY

    def rank_candidates_v22(self, candidates, score_floor):
        ranked = original_rank_candidates(self, candidates, score_floor)
        # rank_score already includes disagreement, venue and H2H penalties.
        # A candidate below 75 after those penalties is not Premium quality.
        return [item for item in ranked if float(item[2]) >= BTTS_V22_MIN_FINAL_RANK]

    DailyPremiumSelector._passes_hard_value_floors = passes_v22
    DailyPremiumSelector._rank_candidates = rank_candidates_v22
    DailyPremiumSelector._btts_v22_installed = True
