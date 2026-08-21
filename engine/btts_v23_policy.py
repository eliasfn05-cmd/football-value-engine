from __future__ import annotations

"""BTTS V2.3 authoritative final-publication policy.

This layer closes selector/ledger bypasses and is the final authority for what
can be rendered as Premium.  BTTS V2.4 venue scoring and V2.5 anti-zero/sample
robustness are invoked here explicitly (without runtime monkey-patching) so
startup and dashboard rendering remain stable.
"""

import re
import unicodedata

BTTS_V23_MIN_EFFECTIVE_RELIABILITY = 0.85
BTTS_V23_MIN_FINAL_RANK = 75.0


def _norm(value) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _lower_liquidity_competition(prediction) -> tuple[bool, str]:
    fixture = getattr(prediction, "fixture", None)
    if fixture is None:
        return False, ""
    ref = getattr(fixture, "competition_ref", None)
    country = _norm(getattr(ref, "country", ""))
    identity = _norm(f"{getattr(fixture, 'competition', '')} {getattr(ref, 'name', '')}")
    if country in {"sweden", "sverige"} and (
        "ettan norra" in identity or "ettan sodra" in identity or identity == "ettan"
    ):
        return True, "lower_league_liquidity_filter:sweden_ettan"
    if "ettan norra" in identity or "ettan sodra" in identity:
        return True, "lower_league_liquidity_filter:sweden_ettan"
    return False, ""


def _v24_block(prediction) -> tuple[bool, str]:
    try:
        from .btts_v24_policy import venue_scoring_decision
        decision = venue_scoring_decision(prediction)
        if decision and decision.blocked:
            return True, decision.code or "btts_v24_venue_scoring_gate"
    except Exception:
        pass
    return False, ""


def _v25_block(prediction) -> tuple[bool, str]:
    try:
        from .btts_v25_policy import anti_zero_decision
        decision = anti_zero_decision(prediction)
        if decision and decision.blocked:
            return True, decision.code or "btts_v25_anti_zero_gate"
    except Exception:
        pass
    return False, ""


def _v25_tier_a_block(prediction) -> tuple[bool, str]:
    try:
        from .btts_v25_policy import tier_a_decision
        decision = tier_a_decision(prediction)
        if decision and decision.blocked:
            return True, decision.code or "btts_v25_tier_a_gate"
    except Exception:
        return True, "btts_v25_tier_a_error"
    return False, ""


def _v25_safety_score(prediction) -> float:
    try:
        from .btts_v25_policy import premium_safety_score
        return float(premium_safety_score(prediction) or 0.0)
    except Exception:
        return 0.0


def _v25_premium_one_safe(prediction) -> bool:
    try:
        from .btts_v25_policy import premium_one_safe
        return bool(premium_one_safe(prediction))
    except Exception:
        return False


def _final_quality(service, prediction) -> tuple[bool, str]:
    lower, lower_reason = _lower_liquidity_competition(prediction)
    if lower:
        return True, lower_reason

    v24_blocked, v24_reason = _v24_block(prediction)
    if v24_blocked:
        return True, v24_reason

    v25_blocked, v25_reason = _v25_block(prediction)
    if v25_blocked:
        return True, v25_reason

    if not service.selector._passes_hard_value_floors(prediction):
        return True, "selector_hard_value_floors"

    calibration = service.selector.calibrator.calibrate(prediction)
    disagreement = service.selector._disagreement_metrics(prediction)
    venue = service.selector._venue_contradiction_metrics(prediction)
    effective_reliability = max(
        0.0,
        float(disagreement.get("effective_reliability") or 0.0)
        - float(venue.get("reliability_penalty") or 0.0),
    )
    if effective_reliability < BTTS_V23_MIN_EFFECTIVE_RELIABILITY:
        return True, f"effective_reliability:{effective_reliability:.3f}<{BTTS_V23_MIN_EFFECTIVE_RELIABILITY:.2f}"

    final_rank, _ = service.selector._rank_score(prediction)
    if float(final_rank) < BTTS_V23_MIN_FINAL_RANK:
        return True, f"final_rank:{float(final_rank):.1f}<{BTTS_V23_MIN_FINAL_RANK:.1f}"

    if str(getattr(prediction, "market", "") or "").strip().upper() != "BTTS":
        return True, "market_excluded:btts_only"

    if float(calibration.reliability or 0.0) < BTTS_V23_MIN_EFFECTIVE_RELIABILITY:
        return True, (
            f"calibration_reliability:{float(calibration.reliability or 0.0):.3f}"
            f"<{BTTS_V23_MIN_EFFECTIVE_RELIABILITY:.2f}"
        )

    # A nominal Tier-A candidate (score >= 90) must additionally prove that
    # both sides are robust scorers and are not being carried by one outlier.
    if float(getattr(prediction, "score", 0.0) or 0.0) >= 90.0:
        tier_a_blocked, tier_a_reason = _v25_tier_a_block(prediction)
        if tier_a_blocked:
            return True, tier_a_reason

    return False, ""


def install_btts_v23_policy() -> None:
    from .premium_replacement import PremiumReplacementService

    if getattr(PremiumReplacementService, "_btts_v23_installed", False):
        return

    original_critical = PremiumReplacementService._critical_consistency_risk

    def critical_v23(self, prediction):
        blocked, reason = original_critical(self, prediction)
        if blocked:
            return blocked, reason
        return _final_quality(self, prediction)

    PremiumReplacementService._critical_consistency_risk = critical_v23
    PremiumReplacementService._btts_v23_installed = True

    try:
        from dashboard.services import DashboardService
        from .premium_risk_guard import PremiumRiskGuard
        from .premium_selection import DailyPremiumSelector

        if not getattr(DashboardService, "_btts_v23_installed", False):
            original_premium_picks = DashboardService.premium_picks

            def premium_picks_v23(self, *, limit=3):
                rows = original_premium_picks(self, limit=max(limit, 30))
                selector = DailyPremiumSelector(model_version=self.model_version, max_picks=3)
                filtered = []
                for row in rows:
                    prediction = row.prediction
                    lower, _ = _lower_liquidity_competition(prediction)
                    if lower:
                        continue
                    v24_blocked, _ = _v24_block(prediction)
                    if v24_blocked:
                        continue
                    v25_blocked, _ = _v25_block(prediction)
                    if v25_blocked:
                        continue
                    risk = PremiumRiskGuard.evaluate(prediction)
                    if risk.blocked:
                        continue
                    if not selector._passes_hard_value_floors(prediction):
                        continue
                    disagreement = selector._disagreement_metrics(prediction)
                    venue = selector._venue_contradiction_metrics(prediction)
                    effective_reliability = max(
                        0.0,
                        float(disagreement.get("effective_reliability") or 0.0)
                        - float(venue.get("reliability_penalty") or 0.0),
                    )
                    if effective_reliability < BTTS_V23_MIN_EFFECTIVE_RELIABILITY:
                        continue
                    calibration = selector.calibrator.calibrate(prediction)
                    if float(calibration.reliability or 0.0) < BTTS_V23_MIN_EFFECTIVE_RELIABILITY:
                        continue
                    final_rank, _ = selector._rank_score(prediction)
                    if float(final_rank) < BTTS_V23_MIN_FINAL_RANK:
                        continue

                    # Tier A must satisfy the stricter V2.5 anti-zero/outlier
                    # evidence.  If not, do not publish it as an A candidate.
                    row_tier = str(getattr(row, "tier", "") or "").strip().upper()
                    if row_tier == "A" or float(getattr(prediction, "score", 0.0) or 0.0) >= 90.0:
                        tier_a_blocked, _ = _v25_tier_a_block(prediction)
                        if tier_a_blocked:
                            continue

                    filtered.append(row)

                # Premium #1 is no longer chosen by generic rank alone.  Sort
                # by the V2.5 safety score first (anti-zero + sample confidence
                # + calibrated BTTS + EV), then by the existing final rank.
                def row_key(row):
                    prediction = row.prediction
                    rank, _ = selector._rank_score(prediction)
                    safe_bonus = 1 if _v25_premium_one_safe(prediction) else 0
                    return (safe_bonus, _v25_safety_score(prediction), float(rank))

                filtered.sort(key=row_key, reverse=True)
                return filtered[:limit]

            DashboardService.premium_picks = premium_picks_v23
            DashboardService._btts_v23_installed = True
    except Exception:
        pass
