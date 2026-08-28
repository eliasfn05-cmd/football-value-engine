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
    """Reconstruct pre-kickoff metrics only.

    anti_zero_metrics() rebuilds role/overall profiles using fixtures with
    kickoff strictly earlier than the audited fixture, so these features do
    not use the audited result.
    """
    m = anti_zero_metrics(prediction)
    if not m.get("available"):
        return None

    home = m["home"]
    away = m["away"]
    home_overall = m["home_overall"]
    away_overall = m["away_overall"]
    weak_side = "home" if _f(home.get("score_probability")) <= _f(away.get("score_probability")) else "away"
    weak = home if weak_side == "home" else away
    weak_overall = home_overall if weak_side == "home" else away_overall

    return {
        "raw_score": _f(getattr(prediction, "score", None)),
        "market_odds": _f(getattr(prediction, "market_odds", None)),
        "expected_value": _f(getattr(prediction, "expected_value", None)),
        "edge": _f(getattr(prediction, "edge", None)),
        "calibrated": _f(m.get("calibrated_probability")),
        "consensus": _f(m.get("consensus_probability")),
        "empirical_btts": _f(m.get("empirical_btts")),
        "weakest_probability": _f(m.get("weakest_score_probability")),
        "weak_last5_scored": _f(weak.get("last5_scored")),
        "weak_last10_scored": _f(weak.get("last10_scored")),
        "weak_last5_btts": _f(weak.get("last5_btts")),
        "weak_overall_last5_scored": _f(weak_overall.get("last5_scored")),
        "weak_overall_last5_btts": _f(weak_overall.get("last5_btts")),
        "weak_score_rate": _f(weak.get("score_rate")),
        "weak_robust_avg_gf": _f(weak.get("robust_avg_gf")),
        "weak_fts": _f(weak.get("failed_to_score_rate")),
    }


def _score_v293(m):
    """Frozen V2.9.3 evidence-first challenger score."""
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


def _risk_v294(m):
    """Frozen audit-only Recent Bilateral Confirmation penalty.

    Thresholds come from the V2 reconstructed loss-pattern audit. This is a
    ranking penalty, not a hard gate, so a candidate can still rank first when
    the rest of its evidence is sufficiently strong.
    """
    penalty = 0.0
    flags = 0

    # Strongest retrospective separator: 0/4 wins versus 4/9 losses.
    if m["weak_overall_last5_btts"] < 4.0:
        penalty += 8.0
        flags += 1

    # Secondary anti-0-0 signal: no reconstructed winner had <4/5 scoring.
    if m["weak_overall_last5_scored"] < 4.0:
        penalty += 5.0
        flags += 1

    # Empirical BTTS is useful but not safe as a hard gate: apply a continuous
    # penalty below .68 instead of discarding the candidate.
    if m["empirical_btts"] < 0.68:
        penalty += min(8.0, (0.68 - m["empirical_btts"]) * 25.0)
        flags += 1

    if m["empirical_btts"] < 0.60:
        penalty += 4.0

    # Interaction: repeated weakness matters more than one isolated warning.
    if flags >= 2:
        penalty += 4.0

    return penalty, flags


def _score_v294(m):
    base = _score_v293(m)
    penalty, flags = _risk_v294(m)
    return base - penalty, penalty, flags


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


def _fmt(s):
    return (
        f"n={s['n']:2d} W={s['wins']:2d} L={s['losses']:2d} "
        f"hit={s['hit']:.4f} roi={s['roi']:+.4f} one={s['one']} 0-0={s['zz']}"
    )


class Command(BaseCommand):
    help = (
        "Walk-forward audit-only de V2.9.4 Recent Bilateral Risk. Compara "
        "V2.9.1 raw A#1, V2.9.3 recalibrado y V2.9.4 con penalizaciones "
        "bilaterales recientes. No cambia produccion."
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
        unavailable = 0
        for p in base:
            if _blocked(tier_a_decision_v291(p)):
                continue
            m = _snapshot(p)
            if m is None:
                unavailable += 1
                continue
            f = p.fixture
            won = f.home_goals > 0 and f.away_goals > 0
            one = not won and ((f.home_goals == 0) != (f.away_goals == 0))
            zz = not won and f.home_goals == 0 and f.away_goals == 0
            v294, risk_penalty, risk_flags = _score_v294(m)
            rows.append({
                "prediction": p,
                "kickoff": f.kickoff,
                "date": f.kickoff.date(),
                "metrics": m,
                "score_v293": _score_v293(m),
                "score_v294": v294,
                "risk_penalty": risk_penalty,
                "risk_flags": risk_flags,
                "won": won,
                "one_sided": one,
                "zero_zero": zz,
            })

        self.stdout.write(self.style.SUCCESS(
            f"BTTS V2.9.4 RECENT BILATERAL RISK WALK-FORWARD | fixtures={len(base)} tier_a={len(rows)} unavailable={unavailable}"
        ))
        self.stdout.write(
            "SOURCE=RECONSTRUCTED_PRE_KICKOFF | audit-only; no gates/ranking de produccion cambian."
        )

        if not rows:
            self.stdout.write(self.style.ERROR("No hay Tier A historicos reconstruibles."))
            return

        days = defaultdict(list)
        for r in rows:
            days[r["date"]].append(r)

        raw_top1, v293_top1, v294_top1 = [], [], []
        changes_293_294 = []
        for day in sorted(days):
            group = days[day]
            raw = max(group, key=lambda r: (r["metrics"]["raw_score"], r["score_v293"]))
            v293 = max(group, key=lambda r: (r["score_v293"], r["metrics"]["raw_score"]))
            v294 = max(group, key=lambda r: (r["score_v294"], r["score_v293"]))
            raw_top1.append(raw)
            v293_top1.append(v293)
            v294_top1.append(v294)
            if v293["prediction"].fixture_id != v294["prediction"].fixture_id:
                changes_293_294.append((day, v293, v294))

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("PREMIUM A#1 DAILY COMPARISON"))
        raw_s = _summary(raw_top1)
        s293 = _summary(v293_top1)
        s294 = _summary(v294_top1)
        self.stdout.write(f"RAW_V291       {_fmt(raw_s)}")
        self.stdout.write(f"RECAL_V293     {_fmt(s293)}")
        self.stdout.write(f"BILAT_V294     {_fmt(s294)}")
        self.stdout.write(
            f"DELTA 294-293   hit={s294['hit'] - s293['hit']:+.4f} "
            f"roi={s294['roi'] - s293['roi']:+.4f} one={s294['one'] - s293['one']:+d} "
            f"0-0={s294['zz'] - s293['zz']:+d} changes={len(changes_293_294)}"
        )

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("RANK CHANGES | V2.9.3 -> V2.9.4"))
        for day, old, new in changes_293_294[:show]:
            of = old["prediction"].fixture
            nf = new["prediction"].fixture
            self.stdout.write(
                f"{day} | OLD {'WIN' if old['won'] else 'LOSS'} {of.home_team.name} vs {of.away_team.name} "
                f"v293={old['score_v293']:.2f} risk=-{old['risk_penalty']:.2f} v294={old['score_v294']:.2f} "
                f"flags={old['risk_flags']} -> NEW {'WIN' if new['won'] else 'LOSS'} "
                f"{nf.home_team.name} vs {nf.away_team.name} v293={new['score_v293']:.2f} "
                f"risk=-{new['risk_penalty']:.2f} v294={new['score_v294']:.2f} flags={new['risk_flags']}"
            )

        n = len(v294_top1)
        w = min(windows, n) if n else 0
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("TEMPORAL WINDOWS | DAILY A#1"))
        stable_hit = 0
        stable_roi = 0
        if w >= 2:
            size = n // w
            rem = n % w
            start = 0
            for i in range(w):
                width = size + (1 if i < rem else 0)
                end = start + width
                c293 = v293_top1[start:end]
                c294 = v294_top1[start:end]
                s_a = _summary(c293)
                s_b = _summary(c294)
                if s_b["hit"] >= s_a["hit"]:
                    stable_hit += 1
                if s_b["roi"] >= s_a["roi"]:
                    stable_roi += 1
                first = c294[0]["date"]
                last = c294[-1]["date"]
                self.stdout.write(
                    f"WINDOW {i + 1} {first}->{last} | V293 {_fmt(s_a)} | V294 {_fmt(s_b)}"
                )
                start = end
        else:
            self.stdout.write("No hay suficientes dias para ventanas temporales.")

        wins = [r for r in rows if r["won"]]
        losses = [r for r in rows if not r["won"]]
        if wins and losses:
            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING("SCORE / RISK SEPARATION"))
            self.stdout.write(
                f"v293_score win_avg={mean(r['score_v293'] for r in wins):.4f} "
                f"loss_avg={mean(r['score_v293'] for r in losses):.4f}"
            )
            self.stdout.write(
                f"v294_score win_avg={mean(r['score_v294'] for r in wins):.4f} "
                f"loss_avg={mean(r['score_v294'] for r in losses):.4f}"
            )
            self.stdout.write(
                f"risk_penalty win_avg={mean(r['risk_penalty'] for r in wins):.4f} "
                f"loss_avg={mean(r['risk_penalty'] for r in losses):.4f}"
            )

        promote_signal = (
            s294["n"] == s293["n"]
            and s294["hit"] >= s293["hit"]
            and s294["roi"] >= s293["roi"]
            and s294["one"] <= s293["one"]
            and s294["zz"] <= s293["zz"]
            and (s294["hit"] > s293["hit"] or s294["roi"] > s293["roi"] or s294["zz"] < s293["zz"])
        )

        self.stdout.write("")
        if promote_signal:
            self.stdout.write(self.style.WARNING(
                "CANDIDATE SIGNAL: V2.9.4 mejora o mantiene hit/ROI y no empeora one-sided/0-0. "
                "NO promover aun: congelar formula y validar en holdout futuro."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                "NO PROMOTION: V2.9.4 no mejora simultaneamente el perfil A#1. Mantener V2.9.1 produccion y V2.9.3 challenger."
            ))
