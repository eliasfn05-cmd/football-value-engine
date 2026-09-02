from __future__ import annotations

from statistics import mean

from django.core.management.base import BaseCommand
from django.db.models import Q

from engine.btts_v25_policy import anti_zero_metrics
from engine.btts_v291_policy import tier_a_decision_v291
from engine.models import Fixture, Prediction


def _f(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _classify(hg: int, ag: int) -> str:
    if hg > 0 and ag > 0:
        return "WIN"
    if hg == 0 and ag == 0:
        return "LOSS_0_0"
    return "LOSS_ONE_SIDED"


def _avg(rows, key):
    vals = [r[key] for r in rows if r.get(key) is not None]
    return mean(vals) if vals else None


def _fmt(value, digits=3):
    return "NA" if value is None else f"{value:.{digits}f}"


def _previous_rest_days(team_id, fixture):
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


def _row(prediction):
    fixture = prediction.fixture
    m = anti_zero_metrics(prediction)
    if not m.get("available"):
        return None

    home = m["home"]
    away = m["away"]
    home_overall = m["home_overall"]
    away_overall = m["away_overall"]

    home_role_season_n = int(home.get("current_season_n", 0) or 0)
    away_role_season_n = int(away.get("current_season_n", 0) or 0)
    min_role_season_n = min(home_role_season_n, away_role_season_n)

    home_overall_season_n = int(home_overall.get("current_season_n", 0) or 0)
    away_overall_season_n = int(away_overall.get("current_season_n", 0) or 0)
    min_overall_season_n = min(home_overall_season_n, away_overall_season_n)

    home_rest = _previous_rest_days(fixture.home_team_id, fixture)
    away_rest = _previous_rest_days(fixture.away_team_id, fixture)
    rest_values = [x for x in (home_rest, away_rest) if x is not None]
    min_rest_days = min(rest_values) if rest_values else None

    hg = int(fixture.home_goals or 0)
    ag = int(fixture.away_goals or 0)

    return {
        "group": _classify(hg, ag),
        "home": fixture.home_team.name,
        "away": fixture.away_team.name,
        "kickoff": fixture.kickoff,
        "scoreline": f"{hg}-{ag}",
        "home_role_season_n": home_role_season_n,
        "away_role_season_n": away_role_season_n,
        "min_role_season_n": min_role_season_n,
        "min_overall_season_n": min_overall_season_n,
        "min_rest_days": min_rest_days,
        "weakest_probability": _f(m.get("weakest_score_probability")),
        "empirical_btts": _f(m.get("empirical_btts")),
        "consensus": _f(m.get("consensus_probability")),
        "calibrated": _f(m.get("calibrated_probability")),
        "max_fts": max(_f(home.get("failed_to_score_rate")) or 0.0, _f(away.get("failed_to_score_rate")) or 0.0),
        "min_last5_scored": min(int(home.get("last5_scored", 0) or 0), int(away.get("last5_scored", 0) or 0)),
        "min_overall_last5_scored": min(int(home_overall.get("last5_scored", 0) or 0), int(away_overall.get("last5_scored", 0) or 0)),
        "min_overall_last5_btts": min(int(home_overall.get("last5_btts", 0) or 0), int(away_overall.get("last5_btts", 0) or 0)),
    }


class Command(BaseCommand):
    help = (
        "V2.9.7 read-only Tier A zero-zero stability audit. Tests whether small "
        "same-season role samples and short recovery windows identify 0-0 losses "
        "without using post-kickoff information as features."
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10000)
        parser.add_argument("--show", type=int, default=50)

    def handle(self, *args, **opts):
        limit = max(1, min(int(opts["limit"]), 50000))
        show = max(1, min(int(opts["show"]), 250))

        candidates = list(
            Prediction.objects.select_related("fixture", "fixture__home_team", "fixture__away_team")
            .filter(
                market__iexact="BTTS",
                fixture__home_goals__isnull=False,
                fixture__away_goals__isnull=False,
            )
            .order_by("-fixture__kickoff", "-created_at")[:limit]
        )

        newest = {}
        for pred in candidates:
            newest.setdefault(pred.fixture_id, pred)

        preds = sorted(newest.values(), key=lambda p: (p.fixture.kickoff, p.id))
        rows = []
        blocked = 0
        unavailable = 0

        for pred in preds:
            if tier_a_decision_v291(pred) is not None:
                blocked += 1
                continue
            row = _row(pred)
            if row is None:
                unavailable += 1
                continue
            rows.append(row)

        groups = {
            "WIN": [r for r in rows if r["group"] == "WIN"],
            "LOSS_0_0": [r for r in rows if r["group"] == "LOSS_0_0"],
            "LOSS_ONE_SIDED": [r for r in rows if r["group"] == "LOSS_ONE_SIDED"],
        }

        self.stdout.write(self.style.SUCCESS(
            "BTTS V2.9.7 ZERO-ZERO STABILITY AUDIT | "
            f"fixtures={len(newest)} tier_a={len(rows)} wins={len(groups['WIN'])} "
            f"zero_zero={len(groups['LOSS_0_0'])} one_sided={len(groups['LOSS_ONE_SIDED'])}"
        ))
        self.stdout.write(
            "SOURCE=COMPLETED_FIXTURES_NEWEST_PREDICTION | PRE-KICKOFF FEATURES ONLY | "
            "same V2.9.1 Tier A replay universe strategy."
        )
        self.stdout.write(
            f"DATA QUALITY | blocked={blocked} unavailable={unavailable}. "
            "READ ONLY: no production gates/ranking changed."
        )
        self.stdout.write(
            "Hypothesis from Thun-Lausanne 0-0: distinguish bilateral scoring strength "
            "from same-season role confidence and recovery/fatigue risk; do not overfit one result.\n"
        )

        keys = (
            "min_role_season_n",
            "min_overall_season_n",
            "min_rest_days",
            "weakest_probability",
            "empirical_btts",
            "consensus",
            "calibrated",
            "max_fts",
            "min_last5_scored",
            "min_overall_last5_scored",
            "min_overall_last5_btts",
        )
        self.stdout.write(self.style.MIGRATE_HEADING("GROUP MEANS"))
        for key in keys:
            self.stdout.write(
                f"{key:30s} WIN={_fmt(_avg(groups['WIN'], key))} "
                f"0-0={_fmt(_avg(groups['LOSS_0_0'], key))} "
                f"ONE={_fmt(_avg(groups['LOSS_ONE_SIDED'], key))}"
            )

        flags = {
            "ROLE_SEASON_SAMPLE_LT3": lambda r: r["min_role_season_n"] < 3,
            "ROLE_SEASON_SAMPLE_LT4": lambda r: r["min_role_season_n"] < 4,
            "OVERALL_SEASON_SAMPLE_LT5": lambda r: r["min_overall_season_n"] < 5,
            "SHORT_REST_LE3_5D": lambda r: r["min_rest_days"] is not None and r["min_rest_days"] <= 3.5,
            "ROLE_LT3_X_SHORT_REST": lambda r: r["min_role_season_n"] < 3 and r["min_rest_days"] is not None and r["min_rest_days"] <= 3.5,
            "ROLE_LT3_X_EMP_LT68": lambda r: r["min_role_season_n"] < 3 and (r["empirical_btts"] or 0.0) < .68,
            "ROLE_LT3_X_RECENT_BTTS_LT4": lambda r: r["min_role_season_n"] < 3 and r["min_overall_last5_btts"] < 4,
            "RECENT_SCORED_LT4": lambda r: r["min_overall_last5_scored"] < 4,
        }

        self.stdout.write("\n" + self.style.MIGRATE_HEADING("CANDIDATE VALIDATION FLAGS"))
        for name, fn in flags.items():
            parts = []
            for group in ("WIN", "LOSS_0_0", "LOSS_ONE_SIDED"):
                rs = groups[group]
                hits = sum(1 for r in rs if fn(r))
                rate = hits / len(rs) if rs else 0.0
                parts.append(f"{group}={hits}/{len(rs)}({rate:.2f})")
            self.stdout.write(f"{name:30s} " + " ".join(parts))

        self.stdout.write("\n" + self.style.MIGRATE_HEADING("ZERO-ZERO DETAILS"))
        for r in groups["LOSS_0_0"][:show]:
            self.stdout.write(
                f"0-0 | {r['home']} vs {r['away']} | {r['kickoff']} | "
                f"roleSeason={r['home_role_season_n']}/{r['away_role_season_n']} "
                f"restMin={_fmt(r['min_rest_days'], 1)}d wp={_fmt(r['weakest_probability'])} "
                f"emp={_fmt(r['empirical_btts'])} cons={_fmt(r['consensus'])} "
                f"recentScored={r['min_overall_last5_scored']}/5 "
                f"recentBTTS={r['min_overall_last5_btts']}/5"
            )

        self.stdout.write("\nDECISION RULE: no hard gate from this audit alone. Promote only a soft/hard validation "
                          "if 0-0 separation is materially higher than WIN exposure with adequate sample size.")
