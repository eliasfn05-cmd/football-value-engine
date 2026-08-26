from __future__ import annotations

from decimal import Decimal

from django.http import JsonResponse

from engine.btts_v27_policy import anti_zero_decision_v27, tier_a_decision_v27
from engine.btts_v291_policy import anti_zero_decision_v291, tier_a_decision_v291
from engine.models import Prediction


def _blocked(decision) -> bool:
    return bool(decision and getattr(decision, "blocked", False))


def _evaluate(qs, decision_fn):
    picks = wins = losses = zero_zero_losses = one_sided_losses = 0
    staked = Decimal("0")
    returned = Decimal("0")
    reasons: dict[str, int] = {}
    examples = []

    for p in qs.iterator(chunk_size=100):
        decision = decision_fn(p)
        if _blocked(decision):
            code = getattr(decision, "code", "blocked")
            reasons[code] = reasons.get(code, 0) + 1
            continue

        f = p.fixture
        if f.home_goals is None or f.away_goals is None:
            continue
        picks += 1
        btts = f.home_goals > 0 and f.away_goals > 0
        if btts:
            wins += 1
        else:
            losses += 1
            if f.home_goals == 0 and f.away_goals == 0:
                zero_zero_losses += 1
            else:
                one_sided_losses += 1

        if p.market_odds is not None and p.market_odds > 0:
            staked += Decimal("1")
            if btts:
                returned += Decimal(p.market_odds)

        if len(examples) < 20:
            examples.append({
                "fixture": f"{f.home_team.name} vs {f.away_team.name}",
                "kickoff": f.kickoff.isoformat(),
                "score": f"{f.home_goals}-{f.away_goals}",
                "win": btts,
                "odds": float(p.market_odds) if p.market_odds is not None else None,
                "probability": float(p.probability),
                "tier": p.tier,
            })

    roi = None
    if staked > 0:
        roi = float((returned - staked) / staked)
    return {
        "picks": picks,
        "wins": wins,
        "losses": losses,
        "hit_rate": round(wins / picks, 4) if picks else None,
        "zero_zero_losses": zero_zero_losses,
        "one_sided_losses": one_sided_losses,
        "roi_flat_1u": round(roi, 4) if roi is not None else None,
        "priced_picks": int(staked),
        "top_rejection_reasons": sorted(reasons.items(), key=lambda x: x[1], reverse=True)[:12],
        "sample_passes": examples,
    }


def btts_v29_vs_v291_backtest(request):
    try:
        limit = max(50, min(int(request.GET.get("limit", "2000")), 10000))
    except ValueError:
        limit = 2000

    qs = (
        Prediction.objects.filter(
            market__iexact="BTTS",
            fixture__home_goals__isnull=False,
            fixture__away_goals__isnull=False,
        )
        .select_related("fixture", "fixture__home_team", "fixture__away_team")
        .order_by("-fixture__kickoff", "-created_at")[:limit]
    )

    # Deduplicate multiple prediction snapshots for the same fixture, keeping the latest.
    unique = []
    seen = set()
    for p in qs:
        if p.fixture_id in seen:
            continue
        seen.add(p.fixture_id)
        unique.append(p.pk)

    base = Prediction.objects.filter(pk__in=unique).select_related(
        "fixture", "fixture__home_team", "fixture__away_team"
    ).order_by("fixture__kickoff")

    v29 = _evaluate(base, anti_zero_decision_v27)
    v291 = _evaluate(base, anti_zero_decision_v291)
    v29_a = _evaluate(base, tier_a_decision_v27)
    v291_a = _evaluate(base, tier_a_decision_v291)

    def delta(a, b, key):
        av, bv = a.get(key), b.get(key)
        if av is None or bv is None:
            return None
        return round(bv - av, 4)

    return JsonResponse({
        "evaluated_unique_fixtures": len(unique),
        "method": "walk-forward policy replay: each policy reads only matches before fixture kickoff; latest stored BTTS prediction per fixture",
        "generic": {
            "v2.9": v29,
            "v2.9.1": v291,
            "delta_v291_minus_v29": {
                "picks": v291["picks"] - v29["picks"],
                "wins": v291["wins"] - v29["wins"],
                "losses": v291["losses"] - v29["losses"],
                "hit_rate": delta(v29, v291, "hit_rate"),
                "roi_flat_1u": delta(v29, v291, "roi_flat_1u"),
                "zero_zero_losses": v291["zero_zero_losses"] - v29["zero_zero_losses"],
            },
        },
        "tier_a": {
            "v2.9": v29_a,
            "v2.9.1": v291_a,
            "delta_v291_minus_v29": {
                "picks": v291_a["picks"] - v29_a["picks"],
                "wins": v291_a["wins"] - v29_a["wins"],
                "losses": v291_a["losses"] - v29_a["losses"],
                "hit_rate": delta(v29_a, v291_a, "hit_rate"),
                "roi_flat_1u": delta(v29_a, v291_a, "roi_flat_1u"),
                "zero_zero_losses": v291_a["zero_zero_losses"] - v29_a["zero_zero_losses"],
            },
        },
    })
