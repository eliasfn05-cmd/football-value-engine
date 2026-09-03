from __future__ import annotations

from collections import defaultdict
from statistics import mean

from django.core.management.base import BaseCommand
from django.db.models import Q

from engine.btts_v25_policy import anti_zero_metrics
from engine.btts_v291_policy import tier_a_decision_v291
from engine.models import Fixture, Prediction


def f(value, default=0.0):
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def blocked(decision):
    return bool(decision and getattr(decision, "blocked", False))


def v293_score(prediction, metrics):
    raw = f(prediction.score)
    emp = f(metrics.get("empirical_btts"))
    cons = f(metrics.get("consensus_probability"))
    cal = f(metrics.get("calibrated_probability"))
    weak = f(metrics.get("weakest_score_probability"))
    score = 100.0 * (.35 * emp + .25 * cons + .20 * cal + .20 * weak)
    if raw >= 85 and emp < .68:
        score -= 10
    if raw >= 85 and cal < .72:
        score -= 4
    if raw >= 90 and cons < .73:
        score -= 4
    if emp >= .80:
        score += 3
    if cons >= .75:
        score += 2
    if weak >= .80:
        score += 2
    return score


def previous_rest_days(team_id, fixture):
    previous = (
        Fixture.objects.filter(
            Q(home_team_id=team_id) | Q(away_team_id=team_id),
            kickoff__lt=fixture.kickoff,
            home_goals__isnull=False,
            away_goals__isnull=False,
        )
        .order_by("-kickoff")
        .only("kickoff")
        .first()
    )
    if not previous or not previous.kickoff:
        return None
    return max(0.0, (fixture.kickoff - previous.kickoff).total_seconds() / 86400.0)


def v298_score(prediction, metrics):
    """Exploratory V2.9.8 score. No production gate is changed here.

    Starts from frozen V2.9.3 and applies only pre-kickoff penalties targeting
    the two dominant residual failure families: 0-0 instability and one-sided
    scoring collapse. Thresholds are intentionally audited before promotion.
    """
    score = v293_score(prediction, metrics)
    home = metrics.get("home") or {}
    away = metrics.get("away") or {}
    ho = metrics.get("home_overall") or {}
    ao = metrics.get("away_overall") or {}

    min_role_season_n = min(int(home.get("current_season_n", 0) or 0), int(away.get("current_season_n", 0) or 0))
    min_recent_scored = min(int(ho.get("last5_scored", 0) or 0), int(ao.get("last5_scored", 0) or 0))
    min_recent_btts = min(int(ho.get("last5_btts", 0) or 0), int(ao.get("last5_btts", 0) or 0))
    max_fts = max(f(home.get("failed_to_score_rate")), f(away.get("failed_to_score_rate")))

    fixture = prediction.fixture
    rests = [
        previous_rest_days(fixture.home_team_id, fixture),
        previous_rest_days(fixture.away_team_id, fixture),
    ]
    rests = [x for x in rests if x is not None]
    min_rest = min(rests) if rests else None

    # Stability / 0-0 risk penalties.
    if min_role_season_n < 3:
        score -= 5
    if min_recent_btts < 4:
        score -= 5
    if min_role_season_n < 3 and min_rest is not None and min_rest <= 3.5:
        score -= 4

    # One-sided collapse penalties.
    if min_recent_scored < 4:
        score -= 7
    if max_fts >= .25:
        score -= 6
    if max_fts >= .35:
        score -= 4

    return score


def outcome(prediction):
    hg = int(prediction.fixture.home_goals or 0)
    ag = int(prediction.fixture.away_goals or 0)
    if hg > 0 and ag > 0:
        return "WIN"
    if hg == 0 and ag == 0:
        return "ZERO_ZERO"
    return "ONE_SIDED"


def summary(rows):
    n = len(rows)
    wins = sum(r["outcome"] == "WIN" for r in rows)
    zz = sum(r["outcome"] == "ZERO_ZERO" for r in rows)
    one = sum(r["outcome"] == "ONE_SIDED" for r in rows)
    priced = [r for r in rows if r["odds"] > 1.0]
    profit = sum((r["odds"] - 1.0) if r["outcome"] == "WIN" else -1.0 for r in priced)
    return {
        "n": n,
        "wins": wins,
        "losses": n - wins,
        "hit": wins / n if n else 0.0,
        "roi": profit / len(priced) if priced else 0.0,
        "zz": zz,
        "one": one,
        "avg_score": mean([r["rank_score"] for r in rows]) if rows else 0.0,
    }


def fmt(s):
    return (
        f"n={s['n']:3d} W={s['wins']:3d} L={s['losses']:3d} hit={s['hit']:.4f} "
        f"roi={s['roi']:+.4f} 0-0={s['zz']:2d} one={s['one']:2d} avgRank={s['avg_score']:.2f}"
    )


class Command(BaseCommand):
    help = (
        "Read-only BTTS audit comparing V2.9.1 raw ranking, frozen V2.9.3 ranking, "
        "and exploratory V2.9.8 stability ranking on the same completed V2.9.1 Tier A universe."
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=20000)
        parser.add_argument("--top", type=int, default=3)
        parser.add_argument("--windows", type=int, default=4)

    def handle(self, *args, **opts):
        limit = max(100, min(int(opts["limit"]), 50000))
        top = max(1, min(int(opts["top"]), 10))
        windows = max(2, min(int(opts["windows"]), 10))

        qs = (
            Prediction.objects.filter(
                market__iexact="BTTS",
                fixture__home_goals__isnull=False,
                fixture__away_goals__isnull=False,
            )
            .select_related("fixture", "fixture__home_team", "fixture__away_team")
            .order_by("-fixture__kickoff", "-created_at")[:limit]
        )

        newest = {}
        for p in qs:
            newest.setdefault(p.fixture_id, p)

        by_day = defaultdict(list)
        unavailable = blocked_n = 0
        for p in newest.values():
            if blocked(tier_a_decision_v291(p)):
                blocked_n += 1
                continue
            metrics = anti_zero_metrics(p)
            if not metrics.get("available"):
                unavailable += 1
                continue
            by_day[p.fixture.kickoff.date()].append((p, metrics))

        versions = {
            "V2.9.1": lambda p, m: f(p.score),
            "V2.9.3": v293_score,
            "V2.9.8": v298_score,
        }
        selected = {name: [] for name in versions}

        for day in sorted(by_day):
            pool = by_day[day]
            for name, scorer in versions.items():
                ranked = sorted(pool, key=lambda pm: (scorer(pm[0], pm[1]), f(pm[0].score), pm[0].id), reverse=True)
                for p, m in ranked[:top]:
                    selected[name].append({
                        "date": day,
                        "prediction": p,
                        "outcome": outcome(p),
                        "odds": f(p.market_odds),
                        "rank_score": scorer(p, m),
                    })

        self.stdout.write(self.style.SUCCESS(
            f"BTTS V2.9.1 vs V2.9.3 vs V2.9.8 RANKING AUDIT | days={len(by_day)} top={top} "
            f"unique_fixtures={len(newest)} blocked={blocked_n} unavailable={unavailable}"
        ))
        self.stdout.write(
            "READ ONLY | same V2.9.1 Tier A eligibility universe | newest completed prediction per fixture | "
            "no production gate/ranking changed."
        )
        self.stdout.write(
            "V2.9.8 hypothesis: keep V2.9.3 bilateral strength ranking, penalize weak recent scoring/BTTS continuity, "
            "failed-to-score exposure, tiny same-season role samples and tiny-sample short-rest interaction."
        )

        overall = {}
        self.stdout.write("\nOVERALL")
        for name in versions:
            overall[name] = summary(selected[name])
            self.stdout.write(f"{name:8s} {fmt(overall[name])}")

        # Chronological day windows. Same dates are used for every version.
        days = sorted(by_day)
        width = max(1, len(days) // windows) if days else 1
        chunks = [days[i:i + width] for i in range(0, len(days), width)]
        if len(chunks) > windows:
            chunks[windows - 1].extend(sum(chunks[windows:], []))
            chunks = chunks[:windows]

        self.stdout.write("\nTEMPORAL WINDOWS")
        nonworse = {name: 0 for name in versions if name != "V2.9.1"}
        for idx, day_chunk in enumerate(chunks, 1):
            if not day_chunk:
                continue
            self.stdout.write(f"WINDOW {idx} | {day_chunk[0]} -> {day_chunk[-1]}")
            stats = {}
            dayset = set(day_chunk)
            for name in versions:
                stats[name] = summary([r for r in selected[name] if r["date"] in dayset])
                self.stdout.write(f"  {name:8s} {fmt(stats[name])}")
            base = stats["V2.9.1"]
            for name in nonworse:
                cand = stats[name]
                if not (cand["hit"] < base["hit"] and cand["roi"] < base["roi"]):
                    nonworse[name] += 1

        base = overall["V2.9.1"]
        self.stdout.write("\nPROMOTION CHECK")
        self.stdout.write(
            "PASS requires: hit > V2.9.1, ROI > V2.9.1, 0-0 <= baseline, one-sided <= baseline, "
            "and >=75% temporal windows non-worse on hit+ROI."
        )
        for name in ("V2.9.3", "V2.9.8"):
            cand = overall[name]
            needed = max(1, int(len(chunks) * .75 + .9999))
            ok = (
                cand["hit"] > base["hit"]
                and cand["roi"] > base["roi"]
                and cand["zz"] <= base["zz"]
                and cand["one"] <= base["one"]
                and nonworse[name] >= needed
            )
            self.stdout.write(
                f"{name:8s} {'PASS' if ok else 'FAIL'} dHit={cand['hit']-base['hit']:+.4f} "
                f"dROI={cand['roi']-base['roi']:+.4f} d0-0={cand['zz']-base['zz']:+d} "
                f"dOne={cand['one']-base['one']:+d} stable={nonworse[name]}/{len(chunks)}"
            )

        self.stdout.write(
            "\nDECISION: V2.9.8 remains audit-only unless it clears the promotion check on the enlarged historical sample. "
            "Do not promote from the Yokohama/Thun/Motherwell block alone."
        )
