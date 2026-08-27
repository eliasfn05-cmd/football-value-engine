from __future__ import annotations

from collections import defaultdict
from statistics import mean

from django.core.management.base import BaseCommand

from engine.btts_v25_policy import anti_zero_metrics
from engine.btts_v291_policy import tier_a_decision_v291
from engine.models import Prediction


def _blocked(decision) -> bool:
    return bool(decision and getattr(decision, "blocked", False))


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _snapshot(prediction):
    m = anti_zero_metrics(prediction)
    if not m.get("available"):
        return None
    home = m["home"]
    away = m["away"]
    weakest_side = "home" if _f(home.get("score_probability")) <= _f(away.get("score_probability")) else "away"
    weak = home if weakest_side == "home" else away
    return {
        "score": _f(getattr(prediction, "score", None)),
        "model_probability": _f(getattr(prediction, "probability", None)),
        "expected_value": _f(getattr(prediction, "expected_value", None)),
        "edge": _f(getattr(prediction, "edge", None)),
        "market_odds": _f(getattr(prediction, "market_odds", None)),
        "weakest_probability": _f(m.get("weakest_score_probability")),
        "consensus": _f(m.get("consensus_probability")),
        "calibrated": _f(m.get("calibrated_probability")),
        "empirical_btts": _f(m.get("empirical_btts")),
        "safety_score": _f(m.get("safety_score")),
        "weak_score_rate": _f(weak.get("score_rate")),
        "weak_btts_rate": _f(weak.get("btts_rate")),
        "weak_fts": _f(weak.get("failed_to_score_rate")),
        "weakest_side": weakest_side,
    }


def _roi(rows):
    priced = [r for r in rows if r["metrics"]["market_odds"] > 1.0]
    if not priced:
        return 0.0
    profit = sum((r["metrics"]["market_odds"] - 1.0) if r["won"] else -1.0 for r in priced)
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


def _bin_score(v):
    if v < 70:
        return "<70"
    if v < 80:
        return "70-79.99"
    if v < 90:
        return "80-89.99"
    return ">=90"


def _bin_prob(v):
    if v < 0.60:
        return "<0.60"
    if v < 0.65:
        return "0.60-0.649"
    if v < 0.70:
        return "0.65-0.699"
    if v < 0.75:
        return "0.70-0.749"
    if v < 0.80:
        return "0.75-0.799"
    return ">=0.80"


def _avg(rows, key):
    vals = [r["metrics"][key] for r in rows]
    return mean(vals) if vals else 0.0


class Command(BaseCommand):
    help = "Audita calibracion del Premium Score V2.9.1 por bins; solo lectura, no modifica produccion."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10000)
        parser.add_argument("--min-bin", type=int, default=2)
        parser.add_argument("--show", type=int, default=50)

    def handle(self, *args, **options):
        limit = max(50, min(int(options["limit"]), 10000))
        min_bin = max(1, min(int(options["min_bin"]), 100))
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
            .order_by("fixture__kickoff")
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
            rows.append({"prediction": p, "metrics": m, "won": won, "one_sided": one, "zero_zero": zz})

        overall = _summary(rows)
        self.stdout.write(self.style.SUCCESS(
            "BTTS V2.9.1 PREMIUM SCORE CALIBRATION AUDIT | "
            f"fixtures={len(base)} tier_a={overall['n']} wins={overall['wins']} losses={overall['losses']}"
        ))
        self.stdout.write("Politica: auditoria solamente; no cambia score, gates ni produccion.")
        self.stdout.write(
            f"BASE | hit={overall['hit']:.4f} roi={overall['roi']:+.4f} one-sided={overall['one']} 0-0={overall['zz']}"
        )

        dimensions = [
            ("SCORE", "score", _bin_score),
            ("MODEL PROB", "model_probability", _bin_prob),
            ("WEAKEST PROB", "weakest_probability", _bin_prob),
            ("CALIBRATED", "calibrated", _bin_prob),
            ("CONSENSUS", "consensus", _bin_prob),
            ("EMPIRICAL BTTS", "empirical_btts", _bin_prob),
        ]

        for title, key, bin_fn in dimensions:
            buckets = defaultdict(list)
            for r in rows:
                buckets[bin_fn(r["metrics"][key])].append(r)
            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING(f"{title} BINS"))
            ordered = sorted(buckets.items(), key=lambda kv: min(x["metrics"][key] for x in kv[1]))
            for label, bucket in ordered:
                if len(bucket) < min_bin:
                    continue
                s = _summary(bucket)
                avg_signal = _avg(bucket, key)
                avg_cal = _avg(bucket, "calibrated")
                cal_gap = avg_cal - s["hit"]
                self.stdout.write(
                    f"{label:12s} n={s['n']:2d} W={s['wins']:2d} L={s['losses']:2d} "
                    f"hit={s['hit']:.4f} roi={s['roi']:+.4f} one={s['one']} 0-0={s['zz']} "
                    f"avg_{key}={avg_signal:.4f} avg_cal={avg_cal:.4f} cal_gap={cal_gap:+.4f}"
                )

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("CROSS BINS | SCORE x CALIBRATED"))
        cross = defaultdict(list)
        for r in rows:
            cross[(_bin_score(r["metrics"]["score"]), _bin_prob(r["metrics"]["calibrated"]))].append(r)
        for (score_bin, cal_bin), bucket in sorted(
            cross.items(), key=lambda kv: (min(x["metrics"]["score"] for x in kv[1]), min(x["metrics"]["calibrated"] for x in kv[1]))
        ):
            if len(bucket) < min_bin:
                continue
            s = _summary(bucket)
            self.stdout.write(
                f"score={score_bin:9s} cal={cal_bin:10s} n={s['n']:2d} W={s['wins']:2d} L={s['losses']:2d} "
                f"hit={s['hit']:.4f} roi={s['roi']:+.4f} one={s['one']} 0-0={s['zz']}"
            )

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("OVERCONFIDENCE FLAGS"))
        flags = [
            ("score>=85 & cal<0.72", lambda m: m["score"] >= 85 and m["calibrated"] < 0.72),
            ("score>=85 & empirical<0.68", lambda m: m["score"] >= 85 and m["empirical_btts"] < 0.68),
            ("score>=85 & weakest<0.76", lambda m: m["score"] >= 85 and m["weakest_probability"] < 0.76),
            ("score>=90 & consensus<0.73", lambda m: m["score"] >= 90 and m["consensus"] < 0.73),
            ("score>=85 & EV<0", lambda m: m["score"] >= 85 and m["expected_value"] < 0.0),
        ]
        for name, fn in flags:
            flagged = [r for r in rows if fn(r["metrics"])]
            if not flagged:
                self.stdout.write(f"{name:32s} n=0")
                continue
            s = _summary(flagged)
            self.stdout.write(
                f"{name:32s} n={s['n']:2d} W={s['wins']:2d} L={s['losses']:2d} "
                f"hit={s['hit']:.4f} roi={s['roi']:+.4f} one={s['one']} 0-0={s['zz']}"
            )

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("TIER A | HIGHEST SCORE FIRST"))
        for r in sorted(rows, key=lambda x: x["metrics"]["score"], reverse=True)[:show]:
            p, f, m = r["prediction"], r["prediction"].fixture, r["metrics"]
            label = "WIN" if r["won"] else ("LOSS-ONE" if r["one_sided"] else "LOSS-00")
            self.stdout.write(
                f"{label:8s} | {f.home_goals}-{f.away_goals} | {f.home_team.name} vs {f.away_team.name} | "
                f"score={m['score']:.2f} cal={m['calibrated']:.3f} cons={m['consensus']:.3f} "
                f"emp={m['empirical_btts']:.3f} weakP={m['weakest_probability']:.3f} "
                f"EV={m['expected_value']:+.4f} odds={m['market_odds']:.2f}"
            )

        self.stdout.write("")
        self.stdout.write(self.style.WARNING(
            "Interpretacion: buscar bins donde el score alto tenga hit real inferior y cal_gap positivo. "
            "No recalibrar produccion hasta validar cualquier ajuste con walk-forward fuera de muestra."
        ))
