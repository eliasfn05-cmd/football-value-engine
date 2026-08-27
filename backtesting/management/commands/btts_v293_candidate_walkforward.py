from __future__ import annotations

from collections import defaultdict

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
    return {
        "expected_value": _f(getattr(prediction, "expected_value", None)),
        "edge": _f(getattr(prediction, "edge", None)),
        "market_odds": _f(getattr(prediction, "market_odds", None)),
        "calibrated": _f(m.get("calibrated_probability")),
        "consensus": _f(m.get("consensus_probability")),
        "empirical_btts": _f(m.get("empirical_btts")),
        "weakest_probability": _f(m.get("weakest_score_probability")),
        "safety_score": _f(m.get("safety_score")),
    }


def _roi(rows):
    priced = [r for r in rows if r["metrics"]["market_odds"] > 1.0]
    if not priced:
        return 0.0
    profit = 0.0
    for r in priced:
        profit += (r["metrics"]["market_odds"] - 1.0) if r["won"] else -1.0
    return profit / len(priced)


def _summary(rows):
    n = len(rows)
    wins = sum(1 for r in rows if r["won"])
    losses = n - wins
    one = sum(1 for r in rows if r["one_sided"])
    zz = sum(1 for r in rows if r["zero_zero"])
    hit = wins / n if n else 0.0
    return {
        "picks": n,
        "wins": wins,
        "losses": losses,
        "hit": hit,
        "roi": _roi(rows),
        "one": one,
        "zz": zz,
    }


def _fmt(s):
    return (
        f"picks={s['picks']:2d} W={s['wins']:2d} L={s['losses']:2d} "
        f"hit={s['hit']:.4f} roi={s['roi']:+.4f} one={s['one']} 0-0={s['zz']}"
    )


class Command(BaseCommand):
    help = (
        "Valida candidatos V2.9.3 con walk-forward temporal sobre Tier A V2.9.1. "
        "No modifica produccion ni promueve ningun gate automaticamente."
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10000)
        parser.add_argument("--windows", type=int, default=4)
        parser.add_argument("--min-retention", type=float, default=0.60)

    def handle(self, *args, **options):
        limit = max(100, min(int(options["limit"]), 10000))
        windows = max(2, min(int(options["windows"]), 10))
        min_retention = max(0.20, min(float(options["min_retention"]), 1.0))

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
                "metrics": m,
                "won": won,
                "one_sided": one,
                "zero_zero": zz,
            })

        candidates = [
            ("BASE_V291", lambda m: True),
            ("EV_GE_0", lambda m: m["expected_value"] >= 0.0),
            ("EV_GE_M002", lambda m: m["expected_value"] >= -0.02),
            ("EV_GE_M005", lambda m: m["expected_value"] >= -0.05),
            (
                "EV0_OR_CAL78_CONS75",
                lambda m: m["expected_value"] >= 0.0
                or (m["calibrated"] >= 0.78 and m["consensus"] >= 0.75),
            ),
            (
                "EVM002_OR_CAL80_CONS77",
                lambda m: m["expected_value"] >= -0.02
                or (m["calibrated"] >= 0.80 and m["consensus"] >= 0.77),
            ),
            (
                "EV0_AND_EMP65",
                lambda m: m["expected_value"] >= 0.0 and m["empirical_btts"] >= 0.65,
            ),
        ]

        self.stdout.write(self.style.SUCCESS(
            f"BTTS V2.9.3 CANDIDATE WALK-FORWARD | fixtures={len(base)} tier_a={len(rows)} windows={windows}"
        ))
        self.stdout.write(
            "Politica: replay temporal; solo informacion previa al kickoff. Auditoria, no cambia produccion."
        )

        if len(rows) < windows:
            self.stdout.write(self.style.ERROR("No hay suficientes Tier A para dividir en ventanas."))
            return

        # Ventanas contiguas y cronologicas para evitar mezclar futuro con pasado.
        size = len(rows) // windows
        remainder = len(rows) % windows
        chunks = []
        start = 0
        for i in range(windows):
            width = size + (1 if i < remainder else 0)
            end = start + width
            chunk = rows[start:end]
            if chunk:
                chunks.append(chunk)
            start = end

        overall = {}
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("OVERALL"))
        for name, fn in candidates:
            kept = [r for r in rows if fn(r["metrics"])]
            s = _summary(kept)
            overall[name] = s
            retention = len(kept) / len(rows) if rows else 0.0
            self.stdout.write(f"{name:24s} {_fmt(s)} retention={retention:.3f}")

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("TEMPORAL WINDOWS"))
        window_stats = defaultdict(list)
        for idx, chunk in enumerate(chunks, start=1):
            first = chunk[0]["kickoff"]
            last = chunk[-1]["kickoff"]
            self.stdout.write(f"\nWINDOW {idx} | {first} -> {last} | baseline_n={len(chunk)}")
            for name, fn in candidates:
                kept = [r for r in chunk if fn(r["metrics"])]
                s = _summary(kept)
                window_stats[name].append(s)
                self.stdout.write(f"  {name:24s} {_fmt(s)}")

        base_s = overall["BASE_V291"]
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("PROMOTION CHECK"))
        self.stdout.write(
            "Criterios: retention>=min, hit global > baseline, ROI global > baseline, "
            "one-sided no aumenta y al menos 2 ventanas no empeoran simultaneamente hit+ROI."
        )

        promoted = []
        for name, _fn in candidates[1:]:
            s = overall[name]
            retention = s["picks"] / base_s["picks"] if base_s["picks"] else 0.0
            hit_better = s["hit"] > base_s["hit"]
            roi_better = s["roi"] > base_s["roi"]
            one_ok = s["one"] <= base_s["one"]

            nonworse_windows = 0
            for cand_w, base_w in zip(window_stats[name], window_stats["BASE_V291"]):
                # Ventana util si no empeora ambas dimensiones a la vez.
                if not (cand_w["hit"] < base_w["hit"] and cand_w["roi"] < base_w["roi"]):
                    nonworse_windows += 1

            stable = nonworse_windows >= min(2, len(chunks))
            ok = retention >= min_retention and hit_better and roi_better and one_ok and stable
            status = "PASS" if ok else "FAIL"
            self.stdout.write(
                f"{name:24s} {status} retention={retention:.3f} "
                f"dHit={s['hit']-base_s['hit']:+.4f} dROI={s['roi']-base_s['roi']:+.4f} "
                f"dOne={s['one']-base_s['one']:+d} stable_windows={nonworse_windows}/{len(chunks)}"
            )
            if ok:
                promoted.append(name)

        self.stdout.write("")
        if promoted:
            self.stdout.write(self.style.WARNING(
                "CANDIDATOS QUE PASAN AUDITORIA: " + ", ".join(promoted)
                + ". Aun requieren revision manual antes de implementar V2.9.3."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                "NO PROMOTION: ningun gate mejora simultaneamente hit/ROI/estabilidad con retencion suficiente. "
                "Mantener V2.9.1 como baseline."
            ))
