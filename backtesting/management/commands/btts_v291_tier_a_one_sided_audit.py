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
    home_overall = m["home_overall"]
    away_overall = m["away_overall"]

    weakest_side = "home" if _f(home.get("score_probability")) <= _f(away.get("score_probability")) else "away"
    weak = home if weakest_side == "home" else away
    weak_overall = home_overall if weakest_side == "home" else away_overall

    return {
        "weakest_side": weakest_side,
        "weakest_probability": _f(m.get("weakest_score_probability")),
        "consensus": _f(m.get("consensus_probability")),
        "calibrated": _f(m.get("calibrated_probability")),
        "empirical_btts": _f(m.get("empirical_btts")),
        "safety_score": _f(m.get("safety_score")),
        "weak_score_rate": _f(weak.get("score_rate")),
        "weak_avg_gf": _f(weak.get("avg_gf")),
        "weak_robust_avg_gf": _f(weak.get("robust_avg_gf")),
        "weak_fts": _f(weak.get("failed_to_score_rate")),
        "weak_btts_rate": _f(weak.get("btts_rate")),
        "weak_last5_scored": _f(weak.get("last5_scored")),
        "weak_last10_scored": _f(weak.get("last10_scored")),
        "weak_last5_btts": _f(weak.get("last5_btts")),
        "weak_outlier_drop": _f(weak.get("outlier_avg_drop")),
        "weak_overall_score_rate": _f(weak_overall.get("score_rate")),
        "weak_overall_last5_scored": _f(weak_overall.get("last5_scored")),
        "weak_overall_last5_btts": _f(weak_overall.get("last5_btts")),
        "market_odds": _f(getattr(prediction, "market_odds", None)),
        "model_probability": _f(getattr(prediction, "probability", None)),
        "edge": _f(getattr(prediction, "edge", None)),
        "expected_value": _f(getattr(prediction, "expected_value", None)),
        "score": _f(getattr(prediction, "score", None)),
    }


def _avg(rows, key):
    values = [row["metrics"][key] for row in rows if row.get("metrics") is not None]
    return mean(values) if values else 0.0


class Command(BaseCommand):
    help = (
        "Audita los Tier A aceptados por BTTS V2.9.1, comparando losses one-sided "
        "contra wins como grupo de control para detectar perfiles de riesgo sin overfitting."
    )

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

        unique = []
        seen = set()
        for prediction in qs:
            if prediction.fixture_id in seen:
                continue
            seen.add(prediction.fixture_id)
            unique.append(prediction.pk)

        base = list(
            Prediction.objects.filter(pk__in=unique)
            .select_related("fixture", "fixture__home_team", "fixture__away_team")
            .order_by("fixture__kickoff")
        )

        accepted = []
        for p in base:
            if _blocked(tier_a_decision_v291(p)):
                continue
            f = p.fixture
            metrics = _snapshot(p)
            if metrics is None:
                continue
            won = f.home_goals > 0 and f.away_goals > 0
            one_sided = not won and (f.home_goals == 0) != (f.away_goals == 0)
            accepted.append({
                "prediction": p,
                "won": won,
                "one_sided": one_sided,
                "metrics": metrics,
            })

        wins = [r for r in accepted if r["won"]]
        losses = [r for r in accepted if r["one_sided"]]
        zero_zero = [r for r in accepted if not r["won"] and not r["one_sided"]]

        self.stdout.write(self.style.SUCCESS(
            f"BTTS V2.9.1 TIER A ONE-SIDED AUDIT | fixtures={len(base)} accepted={len(accepted)}"
        ))
        self.stdout.write(
            f"wins={len(wins)} one_sided_losses={len(losses)} zero_zero_losses={len(zero_zero)}"
        )
        self.stdout.write("Control: todos los perfiles usan solo partidos previos al kickoff.")

        keys = [
            "weakest_probability",
            "consensus",
            "calibrated",
            "empirical_btts",
            "safety_score",
            "weak_score_rate",
            "weak_avg_gf",
            "weak_robust_avg_gf",
            "weak_fts",
            "weak_btts_rate",
            "weak_last5_scored",
            "weak_last10_scored",
            "weak_last5_btts",
            "weak_outlier_drop",
            "weak_overall_score_rate",
            "weak_overall_last5_scored",
            "weak_overall_last5_btts",
            "market_odds",
            "model_probability",
            "edge",
            "expected_value",
            "score",
        ]

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("GROUP COMPARISON | one-sided loss vs wins"))
        for key in keys:
            loss_avg = _avg(losses, key)
            win_avg = _avg(wins, key)
            delta = loss_avg - win_avg
            self.stdout.write(
                f"{key:32s} loss_avg={loss_avg:.4f} win_avg={win_avg:.4f} delta={delta:+.4f}"
            )

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("ONE-SIDED LOSSES"))
        for row in losses[:show]:
            p = row["prediction"]
            f = p.fixture
            m = row["metrics"]
            self.stdout.write(
                "LOSS | {scoreline} | {match} | weak={side} p={wp:.3f} cons={cons:.3f} "
                "cal={cal:.3f} emp={emp:.3f} robustGF={rgf:.2f} scoreRate={sr:.2f} "
                "FTS={fts:.2f} last5={l5:.0f}/5 btts5={b5:.0f}/5 overallSR={osr:.2f} "
                "odds={odds:.2f}".format(
                    scoreline=f"{f.home_goals}-{f.away_goals}",
                    match=f"{f.home_team.name} vs {f.away_team.name}",
                    side=m["weakest_side"], wp=m["weakest_probability"], cons=m["consensus"],
                    cal=m["calibrated"], emp=m["empirical_btts"], rgf=m["weak_robust_avg_gf"],
                    sr=m["weak_score_rate"], fts=m["weak_fts"], l5=m["weak_last5_scored"],
                    b5=m["weak_overall_last5_btts"], osr=m["weak_overall_score_rate"],
                    odds=m["market_odds"],
                )
            )

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("WIN CONTROL"))
        for row in wins[:show]:
            p = row["prediction"]
            f = p.fixture
            m = row["metrics"]
            self.stdout.write(
                "WIN  | {scoreline} | {match} | weak={side} p={wp:.3f} cons={cons:.3f} "
                "cal={cal:.3f} emp={emp:.3f} robustGF={rgf:.2f} scoreRate={sr:.2f} "
                "FTS={fts:.2f} last5={l5:.0f}/5 btts5={b5:.0f}/5 overallSR={osr:.2f} "
                "odds={odds:.2f}".format(
                    scoreline=f"{f.home_goals}-{f.away_goals}",
                    match=f"{f.home_team.name} vs {f.away_team.name}",
                    side=m["weakest_side"], wp=m["weakest_probability"], cons=m["consensus"],
                    cal=m["calibrated"], emp=m["empirical_btts"], rgf=m["weak_robust_avg_gf"],
                    sr=m["weak_score_rate"], fts=m["weak_fts"], l5=m["weak_last5_scored"],
                    b5=m["weak_overall_last5_btts"], osr=m["weak_overall_score_rate"],
                    odds=m["market_odds"],
                )
            )

        self.stdout.write("")
        self.stdout.write(self.style.WARNING(
            "No se cambia ningun umbral automaticamente: primero comparar losses vs wins y luego validar cualquier gate candidato en walk-forward."
        ))
