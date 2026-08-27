from __future__ import annotations

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
        "weakest_side": weakest_side,
        "weakest_probability": _f(m.get("weakest_score_probability")),
        "consensus": _f(m.get("consensus_probability")),
        "calibrated": _f(m.get("calibrated_probability")),
        "empirical_btts": _f(m.get("empirical_btts")),
        "safety_score": _f(m.get("safety_score")),
        "weak_score_rate": _f(weak.get("score_rate")),
        "weak_btts_rate": _f(weak.get("btts_rate")),
        "weak_fts": _f(weak.get("failed_to_score_rate")),
        "market_odds": _f(getattr(prediction, "market_odds", None)),
        "model_probability": _f(getattr(prediction, "probability", None)),
        "edge": _f(getattr(prediction, "edge", None)),
        "expected_value": _f(getattr(prediction, "expected_value", None)),
        "score": _f(getattr(prediction, "score", None)),
    }


def _avg(rows, key):
    vals = [r["metrics"][key] for r in rows]
    return mean(vals) if vals else 0.0


def _roi(rows):
    priced = [r for r in rows if r["metrics"]["market_odds"] > 1.0]
    if not priced:
        return 0.0
    profit = 0.0
    for r in priced:
        profit += (r["metrics"]["market_odds"] - 1.0) if r["won"] else -1.0
    return profit / len(priced)


def _summ(rows):
    wins = sum(1 for r in rows if r["won"])
    losses = len(rows) - wins
    one = sum(1 for r in rows if r["one_sided"])
    zz = sum(1 for r in rows if r["zero_zero"])
    return wins, losses, one, zz, (wins / len(rows) if rows else 0.0), _roi(rows)


class Command(BaseCommand):
    help = "Audita la relacion Score/EV en Tier A V2.9.1 y simula gates candidatos sin modificar produccion."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10000)
        parser.add_argument("--show", type=int, default=50)

    def handle(self, *args, **options):
        limit = max(50, min(int(options["limit"]), 10000))
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

        wins = [r for r in rows if r["won"]]
        losses = [r for r in rows if not r["won"]]

        self.stdout.write(self.style.SUCCESS(
            f"BTTS V2.9.1 SCORE/EV AUDIT | fixtures={len(base)} tier_a={len(rows)} wins={len(wins)} losses={len(losses)}"
        ))
        self.stdout.write("Politica: auditoria solamente; no cambia umbrales ni produccion.")

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("CORE CONTRAST | losses vs wins"))
        for key in ["score", "expected_value", "edge", "market_odds", "model_probability", "weakest_probability", "consensus", "calibrated", "empirical_btts", "safety_score", "weak_score_rate", "weak_btts_rate", "weak_fts"]:
            la, wa = _avg(losses, key), _avg(wins, key)
            self.stdout.write(f"{key:24s} loss_avg={la:.4f} win_avg={wa:.4f} delta={la-wa:+.4f}")

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("CANDIDATE GATES | simulated on accepted Tier A"))
        candidates = [
            ("EV >= 0.00", lambda m: m["expected_value"] >= 0.00),
            ("EV >= -0.02", lambda m: m["expected_value"] >= -0.02),
            ("EV >= -0.05", lambda m: m["expected_value"] >= -0.05),
            ("EV>=0 OR cal>=0.78&cons>=0.75", lambda m: m["expected_value"] >= 0.0 or (m["calibrated"] >= 0.78 and m["consensus"] >= 0.75)),
            ("EV>=-0.02 OR cal>=0.80&cons>=0.77", lambda m: m["expected_value"] >= -0.02 or (m["calibrated"] >= 0.80 and m["consensus"] >= 0.77)),
            ("EV>=0 & empirical>=0.65", lambda m: m["expected_value"] >= 0.0 and m["empirical_btts"] >= 0.65),
        ]
        for name, fn in candidates:
            kept = [r for r in rows if fn(r["metrics"])]
            w, l, one, zz, hit, roi = _summ(kept)
            removed_w = len(wins) - w
            removed_l = len(losses) - l
            self.stdout.write(
                f"{name:38s} picks={len(kept):2d} W={w:2d} L={l:2d} hit={hit:.4f} roi={roi:+.4f} "
                f"one={one} 0-0={zz} removedW={removed_w} removedL={removed_l}"
            )

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("TIER A DETAILS | sorted by EV asc"))
        for r in sorted(rows, key=lambda x: x["metrics"]["expected_value"])[:show]:
            p, f, m = r["prediction"], r["prediction"].fixture, r["metrics"]
            label = "WIN" if r["won"] else ("LOSS-ONE" if r["one_sided"] else "LOSS-00")
            self.stdout.write(
                f"{label:8s} | {f.home_goals}-{f.away_goals} | {f.home_team.name} vs {f.away_team.name} | "
                f"score={m['score']:.2f} EV={m['expected_value']:+.4f} edge={m['edge']:+.4f} odds={m['market_odds']:.2f} "
                f"cal={m['calibrated']:.3f} cons={m['consensus']:.3f} emp={m['empirical_btts']:.3f} weakP={m['weakest_probability']:.3f}"
            )

        self.stdout.write("")
        self.stdout.write(self.style.WARNING(
            "No promover un gate por este audit solamente. El candidato debe mejorar hit/ROI y luego validarse en walk-forward antes de V2.9.3."
        ))
