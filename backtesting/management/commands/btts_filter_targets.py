from __future__ import annotations

import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from engine.btts_v25_policy import anti_zero_metrics
from engine.btts_v2911_policy import v2911_goal_environment_metrics
from engine.btts_v2912_policy import v2912_low_total_metrics
from engine.btts_v2913_policy import anti_zero_decision_v2913, tier_a_decision_v2913
from engine.competition_quality import classify_competition
from engine.models import Fixture, FixtureScoreState, Prediction
from engine.score_v8 import V8_MODEL_VERSION


def norm(value: str | None) -> str:
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii").lower()
    value = re.sub(r"\b(fc|cf|if|club|sc|fk|ac|deportivo|deportes|utd|united)\b", " ", value)
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def sim(a: str, b: str) -> float:
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def decision_text(decision) -> str:
    if decision is None:
        return "PASS"
    return f"BLOCK:{getattr(decision, 'code', 'unknown')}"


class Command(BaseCommand):
    help = "Reusable BTTS target scanner using V2.9.13 ultra-low-total hard guard; odds excluded."

    def add_arguments(self, parser):
        parser.add_argument("--date", required=True, help="Fixture date YYYY-MM-DD")
        parser.add_argument("--targets-file", required=True, help="JSON file with [[home, away], ...] or {targets:[...]}")
        parser.add_argument("--match-threshold", type=float, default=0.68)
        parser.add_argument("--no-score-missing", action="store_true")

    def handle(self, *args, **options):
        day = options["date"]
        target_path = Path(options["targets_file"])
        if not target_path.exists():
            raise CommandError(f"Targets file not found: {target_path}")

        data = json.loads(target_path.read_text(encoding="utf-8"))
        targets = data.get("targets", []) if isinstance(data, dict) else data
        targets = [(str(x[0]), str(x[1])) for x in targets if isinstance(x, (list, tuple)) and len(x) >= 2]
        if not targets:
            raise CommandError("Targets file contains no targets")

        threshold = float(options["match_threshold"])
        fixtures = list(Fixture.objects.filter(kickoff__date=day).select_related("home_team", "away_team", "competition_ref"))
        self.stdout.write(f"BTTS TARGET FILTER | {day} | targets={len(targets)} db_fixtures={len(fixtures)}")
        self.stdout.write(f"POLICY | V2.9.13 ultra-low-total guard | odds excluded | match_threshold={threshold:.2f}")

        matched: list[tuple[str, str, float, Fixture]] = []
        missing = excluded = 0
        for home, away in targets:
            scored = sorted(
                [((sim(home, f.home_team.name) + sim(away, f.away_team.name)) / 2.0, f) for f in fixtures],
                key=lambda x: x[0], reverse=True,
            )
            best, fixture = scored[0] if scored else (0.0, None)
            if fixture is None or best < threshold:
                missing += 1
                self.stdout.write(f"MISSING | {home} vs {away} | best={best:.2f}")
                continue
            if classify_competition(fixture).excluded:
                excluded += 1
                self.stdout.write(f"EXCLUDED | {fixture.home_team.name} vs {fixture.away_team.name} | match={best:.2f}")
                continue
            matched.append((home, away, best, fixture))

        fixture_ids = list({x[3].id for x in matched})
        existing = set(Prediction.objects.filter(fixture_id__in=fixture_ids, market__iexact="BTTS").values_list("fixture_id", flat=True))
        missing_pred_ids = [fid for fid in fixture_ids if fid not in existing]
        score_errors = 0
        if missing_pred_ids and not options["no_score_missing"]:
            self.stdout.write(f"BATCH_SCORE | missing_btts={len(missing_pred_ids)} | scoring date once")
            FixtureScoreState.objects.filter(fixture_id__in=missing_pred_ids, model_version=V8_MODEL_VERSION).delete()
            try:
                call_command("score_v8", date=day, summary_only=True)
            except Exception as exc:
                score_errors += 1
                self.stdout.write(f"BATCH_SCORE_ERROR | {exc.__class__.__name__}: {exc}")

        predictions = (
            Prediction.objects.filter(fixture_id__in=fixture_ids, market__iexact="BTTS")
            .select_related("fixture", "fixture__home_team", "fixture__away_team", "fixture__competition_ref")
            .order_by("fixture_id", "-created_at", "-id")
        )
        latest: dict[int, Prediction] = {}
        for prediction in predictions:
            latest.setdefault(prediction.fixture_id, prediction)

        rows = []
        no_pred = 0
        for _, _, best, fixture in matched:
            prediction = latest.get(fixture.id)
            if prediction is None:
                no_pred += 1
                self.stdout.write(f"NO_PRED | {fixture.home_team.name} vs {fixture.away_team.name} | match={best:.2f}")
                continue

            anti = anti_zero_metrics(prediction)
            goal = v2911_goal_environment_metrics(prediction)
            low = v2912_low_total_metrics(prediction)
            d_a = tier_a_decision_v2913(prediction)
            d_b = anti_zero_decision_v2913(prediction)

            weak = float(anti.get("weakest_score_probability", 0.0) or 0.0)
            cal = float(anti.get("calibrated_probability", 0.0) or 0.0)
            cons = float(anti.get("consensus_probability", 0.0) or 0.0)
            emp = float(anti.get("empirical_btts", 0.0) or 0.0)
            total_lambda = float(goal.get("total_goal_lambda", 0.0) or 0.0)
            p2 = float(goal.get("p_ge_2_goals", 0.0) or 0.0)
            p00 = float(goal.get("p_zero_zero", 1.0) or 1.0)
            weak_lambda = float(goal.get("weakest_goal_lambda", 0.0) or 0.0)
            role5 = int(low.get("min_role_last5_ge2", 0) or 0)
            overall5 = int(low.get("min_overall_last5_ge2", 0) or 0)
            role3 = int(low.get("min_role_last3_ge2", 0) or 0)
            overall3 = int(low.get("min_overall_last3_ge2", 0) or 0)
            role_avg = float(low.get("min_role_avg_total", 0.0) or 0.0)
            overall_avg = float(low.get("min_overall_avg_total", 0.0) or 0.0)
            role_low = int(low.get("max_role_last5_low_total", 5) or 0)
            overall_low = int(low.get("max_overall_last5_low_total", 5) or 0)

            tier = "A" if d_a is None else ("B" if d_b is None else "X")
            rank = (
                (100 if tier == "A" else 65 if tier == "B" else 0)
                + cons * 20 + weak * 20 + cal * 10 + emp * 10 + p2 * 30
                - p00 * 60 + min(total_lambda, 4.5) * 4 + min(weak_lambda, 2.2) * 4
                + role5 * 2 + overall5 * 2 - role_low * 12 - overall_low * 12
            )
            rows.append((rank, tier, fixture, prediction, weak, cal, cons, emp, total_lambda, p2, p00, weak_lambda, role5, overall5, role3, overall3, role_avg, overall_avg, role_low, overall_low, d_a, d_b, best))

        rows.sort(key=lambda x: x[0], reverse=True)
        self.stdout.write("\nRANKED")
        for rank, tier, fixture, prediction, weak, cal, cons, emp, total_lambda, p2, p00, weak_lambda, role5, overall5, role3, overall3, role_avg, overall_avg, role_low, overall_low, d_a, d_b, best in rows:
            self.stdout.write(
                f"{tier} | {fixture.home_team.name} vs {fixture.away_team.name} | rank={rank:.2f} "
                f"prob={float(prediction.probability or 0):.3f} weak={weak:.3f} cal={cal:.3f} cons={cons:.3f} emp={emp:.3f} "
                f"goalLambda={total_lambda:.2f} P2plus={p2:.3f} P00={p00:.3f} weakGoalLambda={weak_lambda:.2f} "
                f"role5_2plus={role5}/5 overall5_2plus={overall5}/5 role3_2plus={role3}/3 overall3_2plus={overall3}/3 "
                f"roleLow5={role_low} overallLow5={overall_low} minRoleAvgTotal={role_avg:.2f} minOverallAvgTotal={overall_avg:.2f} "
                f"v2913A={decision_text(d_a)} v2913B={decision_text(d_b)} match={best:.2f}"
            )

        a_count = sum(1 for r in rows if r[1] == "A")
        b_count = sum(1 for r in rows if r[1] == "B")
        x_count = sum(1 for r in rows if r[1] == "X")
        self.stdout.write(
            f"\nSUMMARY | targets={len(targets)} matched={len(matched)} missing={missing} excluded={excluded} "
            f"no_pred={no_pred} score_errors={score_errors} ranked={len(rows)} A={a_count} B={b_count} X={x_count}"
        )
