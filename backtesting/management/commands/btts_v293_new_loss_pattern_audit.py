from __future__ import annotations

from collections import Counter
from statistics import mean

from django.core.management.base import BaseCommand
from django.db.models import Q

from backtesting.models import PredictionOutcome
from engine.models import PremiumPublicationLedger


class Command(BaseCommand):
    help = "Audit structural patterns separating BTTS wins, 0-0 losses and one-sided losses. Evidence only; no production changes."

    METRICS = (
        "weakest_probability",
        "calibrated_probability",
        "consensus_probability",
        "empirical_btts",
        "weak_score_rate",
        "weak_robust_avg_gf",
        "weak_fts",
        "weak_btts_rate",
        "weak_last5_scored",
        "weak_last10_scored",
        "weak_last5_btts",
        "weak_outlier_drop",
        "weak_overall_score_rate",
        "home_score_rate",
        "away_score_rate",
        "home_robust_avg_gf",
        "away_robust_avg_gf",
        "home_fts",
        "away_fts",
        "home_last5_scored",
        "away_last5_scored",
        "home_outlier_drop",
        "away_outlier_drop",
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10000)
        parser.add_argument("--show", type=int, default=50)
        parser.add_argument("--min-sample", type=int, default=2)

    @staticmethod
    def _f(value):
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _metric(cls, reasons, *keys):
        anti = reasons.get("anti_zero_metrics") or {}
        conversion = reasons.get("conversion_reliability") or {}
        for key in keys:
            for source in (anti, conversion, reasons):
                if key in source:
                    value = cls._f(source.get(key))
                    if value is not None:
                        return value
        return None

    @classmethod
    def _row(cls, ledger, outcome):
        p = ledger.prediction
        f = p.fixture
        reasons = p.reasons or {}
        hg, ag = outcome.home_goals, outcome.away_goals
        if hg is None or ag is None:
            hg, ag = f.home_goals, f.away_goals
        if hg is None or ag is None:
            return None
        if hg > 0 and ag > 0:
            group = "WIN"
        elif hg == 0 and ag == 0:
            group = "LOSS_0_0"
        else:
            group = "LOSS_ONE_SIDED"

        home_sr = cls._metric(reasons, "home_score_rate", "home_scoring_rate")
        away_sr = cls._metric(reasons, "away_score_rate", "away_scoring_rate")
        home_gf = cls._metric(reasons, "home_robust_avg_gf", "home_robust_gf")
        away_gf = cls._metric(reasons, "away_robust_avg_gf", "away_robust_gf")
        home_fts = cls._metric(reasons, "home_fts", "home_fts_rate")
        away_fts = cls._metric(reasons, "away_fts", "away_fts_rate")
        home_l5 = cls._metric(reasons, "home_last5_scored")
        away_l5 = cls._metric(reasons, "away_last5_scored")
        home_drop = cls._metric(reasons, "home_outlier_drop")
        away_drop = cls._metric(reasons, "away_outlier_drop")

        metrics = {
            "weakest_probability": cls._metric(reasons, "weakest_probability", "weakest_link_probability"),
            "calibrated_probability": cls._metric(reasons, "calibrated_probability", "calibrated", "calibrated_btts_probability"),
            "consensus_probability": cls._metric(reasons, "consensus_probability", "consensus", "model_consensus"),
            "empirical_btts": cls._metric(reasons, "empirical_btts", "empirical_btts_rate"),
            "weak_score_rate": cls._metric(reasons, "weak_score_rate"),
            "weak_robust_avg_gf": cls._metric(reasons, "weak_robust_avg_gf"),
            "weak_fts": cls._metric(reasons, "weak_fts"),
            "weak_btts_rate": cls._metric(reasons, "weak_btts_rate"),
            "weak_last5_scored": cls._metric(reasons, "weak_last5_scored"),
            "weak_last10_scored": cls._metric(reasons, "weak_last10_scored"),
            "weak_last5_btts": cls._metric(reasons, "weak_last5_btts"),
            "weak_outlier_drop": cls._metric(reasons, "weak_outlier_drop"),
            "weak_overall_score_rate": cls._metric(reasons, "weak_overall_score_rate"),
            "home_score_rate": home_sr,
            "away_score_rate": away_sr,
            "home_robust_avg_gf": home_gf,
            "away_robust_avg_gf": away_gf,
            "home_fts": home_fts,
            "away_fts": away_fts,
            "home_last5_scored": home_l5,
            "away_last5_scored": away_l5,
            "home_outlier_drop": home_drop,
            "away_outlier_drop": away_drop,
        }
        # Derived candidate signals. These are audited, never enforced here.
        bilateral_sr = min(v for v in (home_sr, away_sr) if v is not None) if any(v is not None for v in (home_sr, away_sr)) else None
        bilateral_gf = min(v for v in (home_gf, away_gf) if v is not None) if any(v is not None for v in (home_gf, away_gf)) else None
        max_fts = max(v for v in (home_fts, away_fts) if v is not None) if any(v is not None for v in (home_fts, away_fts)) else None
        max_drop = max(v for v in (home_drop, away_drop) if v is not None) if any(v is not None for v in (home_drop, away_drop)) else None
        metrics.update({"bilateral_score_floor": bilateral_sr, "bilateral_robust_gf_floor": bilateral_gf, "max_fts": max_fts, "max_outlier_drop": max_drop})

        text = " ".join(str(x or "") for x in (f.competition, f.round)).lower()
        knockout = any(k in text for k in ("cup", "copa", "playoff", "play-off", "knockout", "quarter", "semi", "final", "elimin"))
        first_leg = any(k in text for k in ("1st leg", "first leg", "ida", "leg 1", "1/2"))
        return {
            "group": group, "home": f.home_team.name, "away": f.away_team.name,
            "score": cls._f(p.score), "odds": cls._f(ledger.odds or p.market_odds),
            "ev": cls._f(p.expected_value), "hg": hg, "ag": ag,
            "knockout": knockout, "first_leg": first_leg, **metrics,
        }

    @staticmethod
    def _avg(rows, key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return mean(vals) if vals else None

    def handle(self, *args, **opts):
        limit, show, min_sample = opts["limit"], opts["show"], opts["min_sample"]
        ledgers = PremiumPublicationLedger.objects.select_related(
            "prediction", "prediction__fixture", "prediction__fixture__home_team", "prediction__fixture__away_team"
        ).filter(Q(market__iexact="BTTS") | Q(prediction__market__iexact="BTTS")).order_by("published_at")[:limit]
        rows = []
        for ledger in ledgers:
            try:
                outcome = ledger.prediction.outcome
            except PredictionOutcome.DoesNotExist:
                continue
            if outcome.result not in (PredictionOutcome.RESULT_WIN, PredictionOutcome.RESULT_LOSS):
                continue
            row = self._row(ledger, outcome)
            if row:
                rows.append(row)

        groups = {g: [r for r in rows if r["group"] == g] for g in ("WIN", "LOSS_0_0", "LOSS_ONE_SIDED")}
        self.stdout.write(self.style.SUCCESS(f"BTTS V2.9.3 NEW LOSS PATTERN AUDIT | settled={len(rows)} wins={len(groups['WIN'])} zero_zero={len(groups['LOSS_0_0'])} one_sided={len(groups['LOSS_ONE_SIDED'])}"))
        self.stdout.write("Politica: evidencia solamente; NO cambia gates, ranking ni produccion.\n")

        self.stdout.write(self.style.MIGRATE_HEADING("GROUP METRIC COMPARISON"))
        keys = list(self.METRICS) + ["bilateral_score_floor", "bilateral_robust_gf_floor", "max_fts", "max_outlier_drop", "score", "ev", "odds"]
        for key in keys:
            avgs = {g: self._avg(rs, key) for g, rs in groups.items()}
            if sum(v is not None for v in avgs.values()) < 2:
                continue
            fmt = lambda v: "NA" if v is None else f"{v:.4f}"
            self.stdout.write(f"{key:28s} WIN={fmt(avgs['WIN'])} 0-0={fmt(avgs['LOSS_0_0'])} ONE={fmt(avgs['LOSS_ONE_SIDED'])}")

        self.stdout.write("\n" + self.style.MIGRATE_HEADING("CANDIDATE RISK FLAGS | RETROSPECTIVE"))
        flags = {
            "bilateral_score_floor<0.80": lambda r: r.get("bilateral_score_floor") is not None and r["bilateral_score_floor"] < .80,
            "bilateral_robust_gf<1.50": lambda r: r.get("bilateral_robust_gf_floor") is not None and r["bilateral_robust_gf_floor"] < 1.50,
            "max_fts>=0.20": lambda r: r.get("max_fts") is not None and r["max_fts"] >= .20,
            "max_outlier_drop>=0.18": lambda r: r.get("max_outlier_drop") is not None and r["max_outlier_drop"] >= .18,
            "empirical_btts<0.68": lambda r: r.get("empirical_btts") is not None and r["empirical_btts"] < .68,
            "consensus<0.72": lambda r: r.get("consensus_probability") is not None and r["consensus_probability"] < .72,
            "calibrated<0.72": lambda r: r.get("calibrated_probability") is not None and r["calibrated_probability"] < .72,
            "knockout_context": lambda r: r["knockout"],
            "knockout_first_leg": lambda r: r["knockout"] and r["first_leg"],
        }
        for name, fn in flags.items():
            counts = {g: sum(1 for r in rs if fn(r)) for g, rs in groups.items()}
            rates = {g: (counts[g] / len(groups[g]) if groups[g] else 0) for g in groups}
            self.stdout.write(f"{name:30s} WIN={counts['WIN']}/{len(groups['WIN'])}({rates['WIN']:.2f}) 0-0={counts['LOSS_0_0']}/{len(groups['LOSS_0_0'])}({rates['LOSS_0_0']:.2f}) ONE={counts['LOSS_ONE_SIDED']}/{len(groups['LOSS_ONE_SIDED'])}({rates['LOSS_ONE_SIDED']:.2f})")

        self.stdout.write("\n" + self.style.MIGRATE_HEADING("LOSS DETAILS"))
        losses = [r for r in rows if r["group"] != "WIN"]
        for r in losses[:show]:
            def f(k): return "NA" if r.get(k) is None else f"{r[k]:.3f}"
            self.stdout.write(
                f"{r['group']:14s} | {r['hg']}-{r['ag']} | {r['home']} vs {r['away']} | "
                f"score={f('score')} cal={f('calibrated_probability')} cons={f('consensus_probability')} emp={f('empirical_btts')} "
                f"bilSR={f('bilateral_score_floor')} bilGF={f('bilateral_robust_gf_floor')} maxFTS={f('max_fts')} outDrop={f('max_outlier_drop')} "
                f"EV={f('ev')} odds={f('odds')} KO={int(r['knockout'])} L1={int(r['first_leg'])}"
            )

        self.stdout.write("\n" + self.style.WARNING(
            "INTERPRETACION: buscar flags con tasa claramente mayor en 0-0/one-sided que en WIN. "
            "No promover un filtro con muestra < %d por grupo ni por un solo partido. Si aparece separacion, validarla despues en walk-forward/holdout." % min_sample
        ))
