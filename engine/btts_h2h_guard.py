from __future__ import annotations

from django.db.models import Q

H2H_MIN_SAMPLE = 5
H2H_HARD_MAX_BTTS_RATE = 0.20
H2H_SOFT_MAX_BTTS_RATE = 0.40
H2H_RECENT_WINDOW = 3
H2H_SOFT_MAX_RANK_PENALTY = 8.0


def h2h_metrics(prediction) -> dict:
    """Secondary BTTS H2H guard based only on stored finished fixtures."""
    fixture = prediction.fixture
    home_id, away_id = fixture.home_team_id, fixture.away_team_id
    rows = list(
        fixture.__class__.objects.filter(kickoff__lt=fixture.kickoff)
        .filter(home_goals__isnull=False, away_goals__isnull=False)
        .filter(Q(home_team_id=home_id, away_team_id=away_id) | Q(home_team_id=away_id, away_team_id=home_id))
        .only("kickoff", "home_goals", "away_goals")
        .order_by("-kickoff")[:10]
    )
    n = len(rows)
    if not n:
        return {"available": False, "sample": 0, "blocked": False, "rank_penalty": 0.0}
    flags = [int((r.home_goals or 0) > 0 and (r.away_goals or 0) > 0) for r in rows]
    rate = sum(flags) / n
    recent = flags[:H2H_RECENT_WINDOW]
    recent_all_no = len(recent) == H2H_RECENT_WINDOW and sum(recent) == 0
    blocked = n >= H2H_MIN_SAMPLE and rate <= H2H_HARD_MAX_BTTS_RATE and recent_all_no
    penalty = 0.0
    if n >= H2H_MIN_SAMPLE and not blocked and rate < H2H_SOFT_MAX_BTTS_RATE:
        severity = (H2H_SOFT_MAX_BTTS_RATE - rate) / H2H_SOFT_MAX_BTTS_RATE
        penalty = min(H2H_SOFT_MAX_RANK_PENALTY, severity * H2H_SOFT_MAX_RANK_PENALTY)
    return {"available": True, "sample": n, "btts_rate": round(rate, 3), "recent3_btts": sum(recent), "recent3_all_no": recent_all_no, "blocked": blocked, "rank_penalty": round(penalty, 2), "reason": "h2h_btts_hard_contradiction" if blocked else None}


def install_h2h_guard() -> None:
    from .premium_selection import DailyPremiumSelector
    if getattr(DailyPremiumSelector, "_btts_h2h_guard_installed", False):
        return
    original_passes = DailyPremiumSelector._passes_hard_value_floors.__func__
    original_rejections = DailyPremiumSelector.rejection_reasons.__func__
    original_rank = DailyPremiumSelector._rank_score.__func__

    def passes(cls, prediction):
        return original_passes(cls, prediction) and not h2h_metrics(prediction)["blocked"]

    def rejections(cls, prediction, *, score_floor=76.0):
        out = original_rejections(cls, prediction, score_floor=score_floor)
        h2h = h2h_metrics(prediction)
        if h2h["blocked"]:
            out.append(f"h2h_btts_contradiction:n={h2h['sample']}:rate={h2h['btts_rate']:.3f}:recent3=0")
        return out

    def rank(cls, prediction):
        score, rationale = original_rank(cls, prediction)
        h2h = h2h_metrics(prediction)
        penalty = float(h2h.get("rank_penalty") or 0.0)
        rationale = dict(rationale or {})
        rationale["btts_h2h_guard"] = h2h
        rationale["rank_before_h2h_penalty"] = round(float(score), 2)
        rationale["h2h_rank_penalty"] = round(penalty, 2)
        return max(0.0, float(score) - penalty), rationale

    DailyPremiumSelector._passes_hard_value_floors = classmethod(passes)
    DailyPremiumSelector.rejection_reasons = classmethod(rejections)
    DailyPremiumSelector._rank_score = classmethod(rank)
    DailyPremiumSelector._btts_h2h_guard_installed = True
