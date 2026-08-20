from __future__ import annotations

"""BTTS V2.1 precision policy.

V2 recovered recall after the first BTTS-only version became too restrictive.
V2.1 restores precision without returning to blanket hard floors:

* verified fixture-level contradictions may be vetoed explicitly;
* lower-liquidity third-tier competitions such as Sweden Ettan are excluded;
* one-sided scoring profiles are blocked only when poor attacking output is
  paired with an opponent that is repeatedly keeping clean sheets;
* medium-strength negative H2H evidence becomes a meaningful rank penalty;
* strong negative H2H + weak-side attack can become a hard veto.

The market remains BTTS YES only. Missing H2H is neutral.
"""


def install_btts_v21_policy() -> None:
    from .btts_h2h_guard import h2h_metrics
    from .premium_risk_guard import PremiumRiskDecision, PremiumRiskGuard
    from .premium_selection import DailyPremiumSelector

    if getattr(PremiumRiskGuard, "_btts_v21_installed", False):
        return

    # ------------------------------------------------------------------
    # 1) Verified audit veto: this fixture has independently verified recent
    #    H2H contradiction that our local DB did not contain completely.
    #    This is an audit/data-quality exception, not a team blacklist.
    # ------------------------------------------------------------------
    PremiumRiskGuard.VERIFIED_AUDIT_VETOES.update({
        (
            "2026-08-21",
            "navbahor",
            "nasaf",
            "BTTS",
        ): "verified external H2H contradiction: repeated recent BTTS-NO results; local H2H store incomplete",
    })

    original_evaluate = PremiumRiskGuard.evaluate.__func__

    @classmethod
    def evaluate_v21(cls, prediction):
        base = original_evaluate(cls, prediction)
        if base.blocked:
            return base

        fixture = getattr(prediction, "fixture", None)
        if fixture is None:
            return base

        # Sweden Ettan Norra/Sodra are third-tier, lower-liquidity competitions.
        # They are not appropriate for our Premium layer even if raw BTTS rates
        # are high because price/data quality is materially weaker.
        competition_name = cls._norm(getattr(fixture, "competition", ""))
        ref = getattr(fixture, "competition_ref", None)
        ref_name = cls._norm(getattr(ref, "name", "")) if ref is not None else ""
        competition_identity = f"{competition_name} {ref_name}"
        if "ettan norra" in competition_identity or "ettan sodra" in competition_identity:
            return PremiumRiskDecision(
                True,
                "lower_league_liquidity_filter",
                "Sweden Ettan is third-tier and excluded from Premium BTTS",
            )

        home = cls._current_team_profile(fixture.home_team, fixture)
        away = cls._current_team_profile(fixture.away_team, fixture)

        if home and away:
            # Asymmetric nil-risk: a weak attack facing a defense that is
            # consistently suppressing opponents. Neither fact alone is enough.
            if (
                home["avg_goals_for"] < 0.90
                and home["failed_to_score_rate"] >= 0.40
                and away["clean_sheet_rate"] >= 0.50
            ):
                return PremiumRiskDecision(
                    True,
                    "btts_asymmetric_home_nil_risk",
                    f"home avgGF={home['avg_goals_for']:.2f}, FTS={home['failed_to_score_rate']:.0%}, away CS={away['clean_sheet_rate']:.0%}",
                )
            if (
                away["avg_goals_for"] < 0.90
                and away["failed_to_score_rate"] >= 0.40
                and home["clean_sheet_rate"] >= 0.50
            ):
                return PremiumRiskDecision(
                    True,
                    "btts_asymmetric_away_nil_risk",
                    f"away avgGF={away['avg_goals_for']:.2f}, FTS={away['failed_to_score_rate']:.0%}, home CS={home['clean_sheet_rate']:.0%}",
                )

            # Strong negative H2H plus a weak scoring side is a real structural
            # contradiction. This catches 1-0/2-0 style matchup families without
            # requiring every moderate H2H pattern to be vetoed.
            h2h = h2h_metrics(prediction)
            sample = int(h2h.get("sample") or 0)
            rate = float(h2h.get("btts_rate") or 0.0) if sample else None
            recent3_btts = int(h2h.get("recent3_btts") or 0)
            weak_attack = min(home["avg_goals_for"], away["avg_goals_for"])
            weak_fts = max(home["failed_to_score_rate"], away["failed_to_score_rate"])
            if (
                sample >= 5
                and rate is not None
                and rate <= 0.35
                and recent3_btts <= 1
                and (weak_attack < 1.10 or weak_fts >= 0.40)
            ):
                return PremiumRiskDecision(
                    True,
                    "btts_h2h_plus_attack_contradiction",
                    f"H2H n={sample}, BTTS={rate:.0%}, recent3 BTTS={recent3_btts}, weak avgGF={weak_attack:.2f}, weak FTS={weak_fts:.0%}",
                )

        return base

    PremiumRiskGuard.evaluate = evaluate_v21

    # ------------------------------------------------------------------
    # 2) Ranking: H2H 35-50% is not a hard veto, but it should matter much more
    #    than in V2. Missing H2H remains neutral.
    # ------------------------------------------------------------------
    original_rank = DailyPremiumSelector._rank_score.__func__

    @classmethod
    def rank_v21(cls, prediction):
        score, rationale = original_rank(cls, prediction)
        h2h = h2h_metrics(prediction)
        sample = int(h2h.get("sample") or 0)
        rate = float(h2h.get("btts_rate") or 0.0) if sample else None

        extra_penalty = 0.0
        if sample >= 5 and rate is not None and rate < 0.50:
            # 50% => 0 pts, 40% => 3 pts, 33% => ~5 pts, 20% => 9 pts.
            extra_penalty = min(9.0, max(0.0, (0.50 - rate) / 0.30 * 9.0))

        rationale = dict(rationale or {})
        rationale["btts_v21_h2h_precision"] = {
            "sample": sample,
            "btts_rate": rate,
            "extra_rank_penalty": round(extra_penalty, 2),
        }
        return max(0.0, float(score) - extra_penalty), rationale

    DailyPremiumSelector._rank_score = rank_v21

    PremiumRiskGuard._btts_v21_installed = True
    DailyPremiumSelector._btts_v21_installed = True
