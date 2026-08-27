from __future__ import annotations

from collections import defaultdict
from statistics import mean

from django.core.management.base import BaseCommand

from engine.btts_v25_policy import anti_zero_metrics
from engine.btts_v291_policy import tier_a_decision_v291
from engine.models import Prediction


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _blocked(decision) -> bool:
    return bool(decision and getattr(decision, "blocked", False))


def _snapshot(prediction):
    m = anti_zero_metrics(prediction)
    if not m.get("available"):
        return None
    return {
        "raw_score": _f(getattr(prediction, "score", None)),
        "market_odds": _f(getattr(prediction, "market_odds", None)),
        "expected_value": _f(getattr(prediction, "expected_value", None)),
        "edge": _f(getattr(prediction, "edge", None)),
        "calibrated": _f(m.get("calibrated_probability")),
        "consensus": _f(m.get("consensus_probability")),
        "empirical_btts": _f(m.get("empirical_btts")),
        "weakest_probability": _f(m.get("weakest_score_probability")),
    }


def _candidate_score(m):
    """Evidence-first experimental rank score. Never used by production.

    The audit showed that raw scores >=85 were overconfident while empirical,
    consensus and weakest-link evidence were more useful for separating the
    best Tier A candidates. The formula intentionally keeps the raw score out
    of the positive core and uses it only for overconfidence penalties.
    """
    core = 100.0 * (
        0.35 * m["empirical_btts"]
        + 0.25 * m["consensus"]
        + 0.20 * m["calibrated"]
        + 0.20 * m["weakest_probability"]
    )

    penalty = 0.0
    if m["raw_score"] >= 85.0 and m["empirical_btts"] < 0.68:
        penalty += 10.0
    if m["raw_score"] >= 85.0 and m["calibrated"] < 0.72:
        penalty += 4.0
    if m["raw_score"] >= 90.0 and m["consensus"] < 0.73:
        penalty += 4.0

    bonus = 0.0
    if m["empirical_btts"] >= 0.80:
        bonus += 3.0
    if m["consensus"] >= 0.75:
        bonus += 2.0
    if m["weakest_probability"] >= 0.80:
        bonus += 2.0

    return core - penalty + bonus


def _roi(rows):
    priced = [r for r in rows if r["metrics"]["market_odds"] > 1.0]
    if not priced:
        return 0.0
    profit = sum(
        (r["metrics"]["market_odds"] - 1.0) if r["won"] else -1.0
        for r in priced
    )
    return profit / len(priced)


def _summary(rows):
    n = len(rows)
    wins = sum(1 for r in rows if r["won"])
    one = sum(1 for r in rows if r["one_sided"])
    zz = sum(1 for r in rows if r["zero_zero"])
    return {
        "n": n,
        "wins": wins,
        "losses": n - wins,
        "hit": wins / n if n else 0.0,
        "roi": _roi(rows),
        "one": one,
        "zz": zz,
    }


def _fmt(s):
    return (
        f"n={s['n']:2d} W={s['wins']:2d} L={s['losses']:2d} "
        f"hit={s['hit']:.4f} roi={s['roi']:+.4f} one={s['one']} 0-0={s['zz']}"
    )


class Command(BaseCommand):
    help = (
        "Replay experimental de recalibracion del ranking Premium A. "
        "Compara el A#1 por raw score V2.9.1 contra un score evidence-first. "
        "Solo auditoria: no cambia produccion."
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10000)
        parser.add_argument("--windows", type=int, default=4)
        parser.add_argument("--show", type=int, default=50)

    def handle(self, *args, **options):
        limit = max(100, min(int(options["limit"]), 10000))
        windows = max(2, min(int(options["windows"]), 10))
        show = max(1, min(int(options["show"]), 100))

        qs = (
            Prediction.objects.filter(
                market__iexact="BTTS",
                fixture__home_goals__isnull=False,
                fixture__away_goals__isnull=False,
            )
            .select_related("fixture", "fixture__home_team", "fixture__away_team")
            .order_by("-fixture__kickoff", "-created_at")[:limit]
        )

        unique, seen = [], set()
        for p in qs:
            if p.fixture_id in seen:
                continue
            seen.add(p.fixture_id)
            unique.append(p.pk)

        base = list(
            Prediction.objects.filter(pk__in=unique)
            .select_related("fixture", "fixture__home_team", "fixture__away_team")
            .order_by("fixture__kickoff", "created_at")
        )

        rows = []
        for p in base:
            if _blocked(tier_a_decision_v291(p)):
                continue
            m = _snapshot(p)
            if m is None:
                continue
            f = p.fixture
            won = f.home_goals > 0 and f.away_goals > 0
            one = not won and ((f.home_goals == 0) != (f.away_goals == 0))
            zz = not won and f.home_goals == 0 and f.away_goals == 0
            rows.append({
                "prediction": p,
                "kickoff": f.kickoff,
                "date": f.kickoff.date(),
                "metrics": m,
                "candidate_score": _candidate_score(m),
                "won": won,
                "one_sided": one,
                "zero_zero": zz,
            })

        self.stdout.write(self.style.SUCCESS(
            f"BTTS V2.9.3 SCORE RECALIBRATION REPLAY | fixtures={len(base)} tier_a={len(rows)}"
        ))
        self.stdout.write(
            "Politica: ranking experimental evidence-first; no cambia gates, picks ni produccion."
        )

        if not rows:
            self.stdout.write(self.style.ERROR("No hay Tier A historicos disponibles."))
            return

        days = defaultdict(list)
        for r in rows:
            days[r["date"]].append(r)

        raw_top1, candidate_top1 = [], []
        changes = []
        for day in sorted(days):
            group = days[day]
            raw = max(group, key=lambda r: (r["metrics"]["raw_score"], r["candidate_score"]))
            cand = max(group, key=lambda r: (r["candidate_score"], r["metrics"]["raw_score"]))
            raw_top1.append(raw)
            candidate_top1.append(cand)
            if raw["prediction"].fixture_id != cand["prediction"].fixture_id:
                changes.append((day, raw, cand))

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("PREMIUM A#1 DAILY RANKING"))
        raw_s = _summary(raw_top1)
        cand_s = _summary(candidate_top1)
        self.stdout.write(f"RAW_V291       {_fmt(raw_s)}")
        self.stdout.write(f"RECAL_V293     {_fmt(cand_s)}")
        self.stdout.write(
            f"DELTA           hit={cand_s['hit'] - raw_s['hit']:+.4f} "
            f"roi={cand_s['roi'] - raw_s['roi']:+.4f} "
            f"one={cand_s['one'] - raw_s['one']:+d} changes={len(changes)}"
        )

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("RANK CHANGES | raw A#1 -> recalibrated A#1"))
        for day, raw, cand in changes[:show]:
            rf = raw["prediction"].fixture
            cf = cand["prediction"].fixture
            self.stdout.write(
                f"{day} | RAW {'WIN' if raw['won'] else 'LOSS'} "
                f"{rf.home_team.name} vs {rf.away_team.name} "
                f"raw={raw['metrics']['raw_score']:.2f} recal={raw['candidate_score']:.2f} -> "
                f"NEW {'WIN' if cand['won'] else 'LOSS'} "
                f"{cf.home_team.name} vs {cf.away_team.name} "
                f"raw={cand['metrics']['raw_score']:.2f} recal={cand['candidate_score']:.2f}"
            )

        # Chronological stability by day groups. Ranking is evaluated within each day,
        # then daily A#1 results are split into contiguous windows.
        n = len(candidate_top1)
        w = min(windows, n) if n else 0
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("TEMPORAL WINDOWS | DAILY A#1"))
        if w >= 2:
            size = n // w
            rem = n % w
            start = 0
            raw_stable = 0
            cand_stable = 0
            for i in range(w):
                width = size + (1 if i < rem else 0)
                end = start + width
                raw_chunk = raw_top1[start:end]
                cand_chunk = candidate_top1[start:end]
                rs = _summary(raw_chunk)
                cs = _summary(cand_chunk)
                if cs["hit"] >= rs["hit"]:
                    cand_stable += 1
                if cs["roi"] >= rs["roi"]:
                    raw_stable += 1
                first = cand_chunk[0]["date"]
                last = cand_chunk[-1]["date"]
                self.stdout.write(
                    f"WINDOW {i + 1} {first}->{last} | RAW {_fmt(rs)} | RECAL {_fmt(cs)}"
                )
                start = end
        else:
            self.stdout.write("No hay suficientes dias para ventanas temporales.")

        # Diagnostic: rank correlation direction using simple win/loss means.
        win_rows = [r for r in rows if r["won"]]
        loss_rows = [r for r in rows if not r["won"]]
        if win_rows and loss_rows:
            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING("SCORE SEPARATION"))
            self.stdout.write(
                f"raw_score     win_avg={mean(r['metrics']['raw_score'] for r in win_rows):.4f} "
                f"loss_avg={mean(r['metrics']['raw_score'] for r in loss_rows):.4f}"
            )
            self.stdout.write(
                f"recal_score   win_avg={mean(r['candidate_score'] for r in win_rows):.4f} "
                f"loss_avg={mean(r['candidate_score'] for r in loss_rows):.4f}"
            )

        promote = (
            cand_s["n"] == raw_s["n"]
            and cand_s["hit"] > raw_s["hit"]
            and cand_s["roi"] >= raw_s["roi"]
            and cand_s["one"] <= raw_s["one"]
        )
        self.stdout.write("")
        if promote:
            self.stdout.write(self.style.WARNING(
                "CANDIDATE SIGNAL: mejora el A#1 retrospectivo. NO promover aun: validar en muestra futura/holdout antes de produccion."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                "NO PROMOTION: la recalibracion no mejora simultaneamente hit/ROI/one-sided del A#1. Mantener V2.9.1."
            ))
