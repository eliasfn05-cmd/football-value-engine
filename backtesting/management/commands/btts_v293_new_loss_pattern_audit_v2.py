from __future__ import annotations

from statistics import mean

from django.core.management.base import BaseCommand
from django.db.models import Q

from backtesting.models import PredictionOutcome
from engine.btts_v25_policy import anti_zero_metrics
from engine.models import PremiumPublicationLedger


GROUPS = ("WIN", "LOSS_0_0", "LOSS_ONE_SIDED")


def _f(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _avg(rows, key):
    values = [r[key] for r in rows if r.get(key) is not None]
    return mean(values) if values else None


def _fmt(value, digits=4):
    return "NA" if value is None else f"{value:.{digits}f}"


def _classify(home_goals, away_goals):
    if home_goals > 0 and away_goals > 0:
        return "WIN"
    if home_goals == 0 and away_goals == 0:
        return "LOSS_0_0"
    return "LOSS_ONE_SIDED"


def _knockout_flags(fixture):
    text = " ".join(str(x or "") for x in (fixture.competition, fixture.round)).lower()
    knockout = any(
        token in text
        for token in (
            "cup", "copa", "playoff", "play-off", "knockout", "quarter",
            "semi", "final", "elimin", "cuartos", "semifinal",
        )
    )
    first_leg = any(
        token in text
        for token in ("1st leg", "first leg", "ida", "leg 1", "1/2", "first-leg")
    )
    return knockout, first_leg


def _reconstructed_metrics(prediction):
    """Rebuild V2.5/V2.9.x anti-zero evidence at the fixture cutoff.

    anti_zero_metrics() queries only completed fixtures with kickoff strictly
    earlier than the target fixture. Therefore the role/overall profiles are
    leakage-safe and reproduce the same robust scoring logic used by the
    production BTTS policy: venue score rate, FTS, robust GF, recent scoring,
    BTTS participation, outlier dependence, calibrated probability, empirical
    BTTS and weakest-link consensus.
    """
    metrics = anti_zero_metrics(prediction)
    if not metrics.get("available"):
        return None

    home = metrics["home"]
    away = metrics["away"]
    home_overall = metrics["home_overall"]
    away_overall = metrics["away_overall"]

    weakest_side = (
        "home"
        if _f(home.get("score_probability")) <= _f(away.get("score_probability"))
        else "away"
    )
    weak = home if weakest_side == "home" else away
    weak_overall = home_overall if weakest_side == "home" else away_overall

    home_sr = _f(home.get("score_rate"))
    away_sr = _f(away.get("score_rate"))
    home_gf = _f(home.get("robust_avg_gf"))
    away_gf = _f(away.get("robust_avg_gf"))
    home_fts = _f(home.get("failed_to_score_rate"))
    away_fts = _f(away.get("failed_to_score_rate"))
    home_drop = _f(home.get("outlier_avg_drop"))
    away_drop = _f(away.get("outlier_avg_drop"))

    return {
        "weakest_side": weakest_side,
        "weakest_probability": _f(metrics.get("weakest_score_probability")),
        "calibrated_probability": _f(metrics.get("calibrated_probability")),
        "consensus_probability": _f(metrics.get("consensus_probability")),
        "empirical_btts": _f(metrics.get("empirical_btts")),
        "safety_score": _f(metrics.get("safety_score")),
        "weak_score_rate": _f(weak.get("score_rate")),
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
        "home_n": _f(home.get("n")),
        "away_n": _f(away.get("n")),
        "home_score_rate": home_sr,
        "away_score_rate": away_sr,
        "home_robust_avg_gf": home_gf,
        "away_robust_avg_gf": away_gf,
        "home_fts": home_fts,
        "away_fts": away_fts,
        "home_btts_rate": _f(home.get("btts_rate")),
        "away_btts_rate": _f(away.get("btts_rate")),
        "home_last5_scored": _f(home.get("last5_scored")),
        "away_last5_scored": _f(away.get("last5_scored")),
        "home_last10_scored": _f(home.get("last10_scored")),
        "away_last10_scored": _f(away.get("last10_scored")),
        "home_last5_btts": _f(home.get("last5_btts")),
        "away_last5_btts": _f(away.get("last5_btts")),
        "home_outlier_drop": home_drop,
        "away_outlier_drop": away_drop,
        "home_overall_score_rate": _f(home_overall.get("score_rate")),
        "away_overall_score_rate": _f(away_overall.get("score_rate")),
        "home_overall_last5_scored": _f(home_overall.get("last5_scored")),
        "away_overall_last5_scored": _f(away_overall.get("last5_scored")),
        "home_overall_last5_btts": _f(home_overall.get("last5_btts")),
        "away_overall_last5_btts": _f(away_overall.get("last5_btts")),
        "bilateral_score_floor": min(home_sr, away_sr),
        "bilateral_robust_gf_floor": min(home_gf, away_gf),
        "max_fts": max(home_fts, away_fts),
        "max_outlier_drop": max(home_drop, away_drop),
    }


class Command(BaseCommand):
    help = (
        "BTTS V2.9.3 loss-pattern audit V2. Reconstructs pre-kickoff anti-zero "
        "metrics from historical fixtures, avoiding Prediction.reasons gaps and leakage."
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10000)
        parser.add_argument("--show", type=int, default=50)
        parser.add_argument("--min-sample", type=int, default=2)

    def handle(self, *args, **opts):
        limit = max(1, min(int(opts["limit"]), 50000))
        show = max(1, min(int(opts["show"]), 250))
        min_sample = max(1, int(opts["min_sample"]))

        ledgers = list(
            PremiumPublicationLedger.objects.select_related(
                "prediction",
                "prediction__fixture",
                "prediction__fixture__home_team",
                "prediction__fixture__away_team",
            )
            .filter(Q(market__iexact="BTTS") | Q(prediction__market__iexact="BTTS"))
            .order_by("published_at")[:limit]
        )

        rows = []
        skipped_pending = 0
        skipped_no_score = 0
        unavailable = 0

        for ledger in ledgers:
            prediction = ledger.prediction
            fixture = prediction.fixture
            try:
                outcome = prediction.outcome
            except PredictionOutcome.DoesNotExist:
                skipped_pending += 1
                continue
            if outcome.result not in (PredictionOutcome.RESULT_WIN, PredictionOutcome.RESULT_LOSS):
                skipped_pending += 1
                continue

            hg = outcome.home_goals
            ag = outcome.away_goals
            if hg is None or ag is None:
                hg, ag = fixture.home_goals, fixture.away_goals
            if hg is None or ag is None:
                skipped_no_score += 1
                continue

            rebuilt = _reconstructed_metrics(prediction)
            if rebuilt is None:
                unavailable += 1
                continue

            knockout, first_leg = _knockout_flags(fixture)
            rows.append({
                "group": _classify(int(hg), int(ag)),
                "home": fixture.home_team.name,
                "away": fixture.away_team.name,
                "hg": int(hg),
                "ag": int(ag),
                "kickoff": fixture.kickoff,
                "score": _f(prediction.score),
                "model_probability": _f(prediction.probability),
                "edge": _f(prediction.edge),
                "ev": _f(prediction.expected_value),
                "odds": _f(ledger.odds or prediction.market_odds),
                "knockout": knockout,
                "first_leg": first_leg,
                **rebuilt,
            })

        groups = {g: [r for r in rows if r["group"] == g] for g in GROUPS}

        self.stdout.write(self.style.SUCCESS(
            "BTTS V2.9.3 NEW LOSS PATTERN AUDIT V2 | "
            f"settled={len(rows)} wins={len(groups['WIN'])} "
            f"zero_zero={len(groups['LOSS_0_0'])} one_sided={len(groups['LOSS_ONE_SIDED'])}"
        ))
        self.stdout.write(
            "SOURCE=RECONSTRUCTED_PRE_KICKOFF | anti_zero_metrics() | "
            "solo fixtures anteriores al kickoff; no usa resultado futuro para features."
        )
        self.stdout.write(
            f"DATA QUALITY | ledgers={len(ledgers)} pending/void={skipped_pending} "
            f"no_score={skipped_no_score} reconstruction_unavailable={unavailable}"
        )
        self.stdout.write("Politica: auditoria solamente; NO cambia gates, ranking ni produccion.\n")

        comparison_keys = [
            "weakest_probability",
            "calibrated_probability",
            "consensus_probability",
            "empirical_btts",
            "safety_score",
            "bilateral_score_floor",
            "bilateral_robust_gf_floor",
            "max_fts",
            "max_outlier_drop",
            "weak_score_rate",
            "weak_robust_avg_gf",
            "weak_fts",
            "weak_btts_rate",
            "weak_last5_scored",
            "weak_last10_scored",
            "weak_last5_btts",
            "weak_overall_score_rate",
            "weak_overall_last5_scored",
            "weak_overall_last5_btts",
            "model_probability",
            "score",
            "edge",
            "ev",
            "odds",
        ]

        self.stdout.write(self.style.MIGRATE_HEADING("GROUP METRIC COMPARISON | RECONSTRUCTED"))
        for key in comparison_keys:
            vals = {g: _avg(rs, key) for g, rs in groups.items()}
            if sum(v is not None for v in vals.values()) < 2:
                continue
            self.stdout.write(
                f"{key:32s} WIN={_fmt(vals['WIN'])} "
                f"0-0={_fmt(vals['LOSS_0_0'])} ONE={_fmt(vals['LOSS_ONE_SIDED'])}"
            )

        self.stdout.write("\n" + self.style.MIGRATE_HEADING("CANDIDATE RISK FLAGS | RETROSPECTIVE"))
        flags = {
            "bilateral_score_floor<0.80": lambda r: r["bilateral_score_floor"] < .80,
            "bilateral_score_floor<0.70": lambda r: r["bilateral_score_floor"] < .70,
            "bilateral_robust_gf<1.50": lambda r: r["bilateral_robust_gf_floor"] < 1.50,
            "bilateral_robust_gf<1.30": lambda r: r["bilateral_robust_gf_floor"] < 1.30,
            "max_fts>=0.20": lambda r: r["max_fts"] >= .20,
            "max_fts>=0.30": lambda r: r["max_fts"] >= .30,
            "max_outlier_drop>=0.18": lambda r: r["max_outlier_drop"] >= .18,
            "empirical_btts<0.68": lambda r: r["empirical_btts"] < .68,
            "empirical_btts<0.60": lambda r: r["empirical_btts"] < .60,
            "consensus<0.72": lambda r: r["consensus_probability"] < .72,
            "consensus<0.65": lambda r: r["consensus_probability"] < .65,
            "calibrated<0.72": lambda r: r["calibrated_probability"] < .72,
            "weak_last5_scored<4": lambda r: r["weak_last5_scored"] < 4,
            "weak_last10_scored<8": lambda r: r["weak_last10_scored"] < 8,
            "weak_last5_btts<4": lambda r: r["weak_last5_btts"] < 4,
            "knockout_context": lambda r: r["knockout"],
            "knockout_first_leg": lambda r: r["knockout"] and r["first_leg"],
        }
        for name, fn in flags.items():
            pieces = []
            for group in GROUPS:
                rs = groups[group]
                count = sum(1 for r in rs if fn(r))
                rate = count / len(rs) if rs else 0.0
                pieces.append(f"{group}={count}/{len(rs)}({rate:.2f})")
            self.stdout.write(f"{name:32s} " + " ".join(pieces))

        self.stdout.write("\n" + self.style.MIGRATE_HEADING("LOSS DETAILS | RECONSTRUCTED"))
        losses = [r for r in rows if r["group"] != "WIN"]
        for r in losses[:show]:
            self.stdout.write(
                f"{r['group']:15s} | {r['hg']}-{r['ag']} | {r['home']} vs {r['away']} | "
                f"weak={r['weakest_side']} wp={_fmt(r['weakest_probability'], 3)} "
                f"cal={_fmt(r['calibrated_probability'], 3)} cons={_fmt(r['consensus_probability'], 3)} "
                f"emp={_fmt(r['empirical_btts'], 3)} bilSR={_fmt(r['bilateral_score_floor'], 3)} "
                f"bilGF={_fmt(r['bilateral_robust_gf_floor'], 2)} maxFTS={_fmt(r['max_fts'], 3)} "
                f"outDrop={_fmt(r['max_outlier_drop'], 3)} weakL5={_fmt(r['weak_last5_scored'], 0)}/5 "
                f"weakL10={_fmt(r['weak_last10_scored'], 0)}/10 weakBTTS5={_fmt(r['weak_last5_btts'], 0)}/5 "
                f"score={_fmt(r['score'], 2)} EV={_fmt(r['ev'], 3)} odds={_fmt(r['odds'], 2)} "
                f"KO={int(r['knockout'])} L1={int(r['first_leg'])}"
            )

        self.stdout.write("\n" + self.style.MIGRATE_HEADING("WIN CONTROL | RECONSTRUCTED"))
        for r in groups["WIN"][:show]:
            self.stdout.write(
                f"WIN | {r['hg']}-{r['ag']} | {r['home']} vs {r['away']} | "
                f"weak={r['weakest_side']} wp={_fmt(r['weakest_probability'], 3)} "
                f"cal={_fmt(r['calibrated_probability'], 3)} cons={_fmt(r['consensus_probability'], 3)} "
                f"emp={_fmt(r['empirical_btts'], 3)} bilSR={_fmt(r['bilateral_score_floor'], 3)} "
                f"bilGF={_fmt(r['bilateral_robust_gf_floor'], 2)} maxFTS={_fmt(r['max_fts'], 3)} "
                f"outDrop={_fmt(r['max_outlier_drop'], 3)} weakL5={_fmt(r['weak_last5_scored'], 0)}/5 "
                f"weakL10={_fmt(r['weak_last10_scored'], 0)}/10 weakBTTS5={_fmt(r['weak_last5_btts'], 0)}/5"
            )

        self.stdout.write("\n" + self.style.WARNING(
            "INTERPRETACION: solo considerar un flag candidato si separa losses de WIN de forma repetida. "
            f"No promover filtros con muestra < {min_sample} por grupo ni por un unico partido; "
            "cualquier candidato debe pasar despues walk-forward/holdout."
        ))
