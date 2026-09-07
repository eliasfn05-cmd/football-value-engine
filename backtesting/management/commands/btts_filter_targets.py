from __future__ import annotations

import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from engine.btts_v25_policy import anti_zero_metrics
from engine.btts_v299_policy import tier_a_decision_v299, anti_zero_decision_v299
from engine.btts_v2910_policy import tier_a_decision_v2910, anti_zero_decision_v2910
from engine.btts_v2911_policy import (
    tier_a_decision_v2911,
    anti_zero_decision_v2911,
    v2911_goal_environment_metrics,
)
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
    help = "Reusable BTTS screenshot/target scanner using V2.9.11 hard anti-zero/two-goal policy with odds excluded."

    def add_arguments(self, parser):
        parser.add_argument("--date", required=True, help="Fixture date YYYY-MM-DD")
        parser.add_argument("--targets-file", required=True, help="JSON file with [[home, away], ...] or {targets:[...]}.")
        parser.add_argument("--match-threshold", type=float, default=0.68)
        parser.add_argument("--no-score-missing", action="store_true", help="Do not invoke batch Score V8 for missing BTTS predictions.")

    def handle(self, *args, **options):
        day = options["date"]
        target_path = Path(options["targets_file"])
        if not target_path.exists():
            raise CommandError(f"Targets file not found: {target_path}")
        data = json.loads(target_path.read_text(encoding="utf-8"))
        targets = data.get("targets", []) if isinstance(data, dict) else data
        if not targets:
            raise CommandError("Targets file contains no targets")
        targets = [(str(x[0]), str(x[1])) for x in targets if isinstance(x, (list, tuple)) and len(x) >= 2]
        threshold = float(options["match_threshold"])

        fixtures = list(Fixture.objects.filter(kickoff__date=day).select_related("home_team", "away_team", "competition_ref"))
        self.stdout.write(f"BTTS TARGET FILTER | {day} | targets={len(targets)} db_fixtures={len(fixtures)}")
        self.stdout.write(f"POLICY | V2.9.11 hard anti-zero + two-goal floor | odds excluded | match_threshold={threshold:.2f}")

        matched_targets: list[tuple[str, str, float, Fixture]] = []
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
            matched_targets.append((home, away, best, fixture))

        matched_fixture_ids = list({item[3].id for item in matched_targets})
        existing_btts_fixture_ids = set(
            Prediction.objects.filter(fixture_id__in=matched_fixture_ids, market__iexact="BTTS")
            .values_list("fixture_id", flat=True)
        )
        missing_pred_fixture_ids = [fid for fid in matched_fixture_ids if fid not in existing_btts_fixture_ids]

        score_errors = 0
        if missing_pred_fixture_ids and not options["no_score_missing"]:
            self.stdout.write(
                f"BATCH_SCORE | missing_btts={len(missing_pred_fixture_ids)} | clearing stale V8 state and scoring date once"
            )
            FixtureScoreState.objects.filter(
                fixture_id__in=missing_pred_fixture_ids,
                model_version=V8_MODEL_VERSION,
            ).delete()
            try:
                call_command("score_v8", date=day, summary_only=True)
            except Exception as exc:
                score_errors += 1
                self.stdout.write(f"BATCH_SCORE_ERROR | {exc.__class__.__name__}: {exc}")

        predictions = (
            Prediction.objects.filter(fixture_id__in=matched_fixture_ids, market__iexact="BTTS")
            .select_related("fixture", "fixture__home_team", "fixture__away_team", "fixture__competition_ref")
            .order_by("fixture_id", "-created_at", "-id")
        )
        latest_by_fixture: dict[int, Prediction] = {}
        for prediction in predictions:
            latest_by_fixture.setdefault(prediction.fixture_id, prediction)

        rows = []
        no_pred = 0
        for home, away, best, fixture in matched_targets:
            prediction = latest_by_fixture.get(fixture.id)
            if prediction is None:
                no_pred += 1
                self.stdout.write(f"NO_PRED | {fixture.home_team.name} vs {fixture.away_team.name} | match={best:.2f}")
                continue

            metrics = anti_zero_metrics(prediction)
            goal_env = v2911_goal_environment_metrics(prediction)
            d299a = tier_a_decision_v299(prediction)
            d299b = anti_zero_decision_v299(prediction)
            d2910a = tier_a_decision_v2910(prediction)
            d2910b = anti_zero_decision_v2910(prediction)
            d2911a = tier_a_decision_v2911(prediction)
            d2911b = anti_zero_decision_v2911(prediction)
            available = bool(metrics.get("available"))
            weak = float(metrics.get("weakest_score_probability", 0.0) or 0.0)
            cal = float(metrics.get("calibrated_probability", 0.0) or 0.0)
            cons = float(metrics.get("consensus_probability", 0.0) or 0.0)
            emp = float(metrics.get("empirical_btts", 0.0) or 0.0)
            max_zero = float(metrics.get("max_zero_risk", 1.0) or 1.0)
            home_o = metrics.get("home_overall") or {}
            away_o = metrics.get("away_overall") or {}
            home_r = metrics.get("home") or {}
            away_r = metrics.get("away") or {}
            fts = max(float(home_o.get("failed_to_score_rate", 1.0) or 0.0), float(away_o.get("failed_to_score_rate", 1.0) or 0.0)) if available else 1.0
            role_fts = max(float(home_r.get("failed_to_score_rate", 1.0) or 0.0), float(away_r.get("failed_to_score_rate", 1.0) or 0.0)) if available else 1.0
            l5s = min(int(home_o.get("last5_scored", 0) or 0), int(away_o.get("last5_scored", 0) or 0)) if available else 0
            role_l5s = min(int(home_r.get("last5_scored", 0) or 0), int(away_r.get("last5_scored", 0) or 0)) if available else 0
            l5b = min(int(home_o.get("last5_btts", 0) or 0), int(away_o.get("last5_btts", 0) or 0)) if available else 0
            total_lambda = float(goal_env.get("total_goal_lambda", 0.0) or 0.0)
            p2 = float(goal_env.get("p_ge_2_goals", 0.0) or 0.0)
            p00 = float(goal_env.get("p_zero_zero", 1.0) or 1.0)
            weak_lambda = float(goal_env.get("weakest_goal_lambda", 0.0) or 0.0)

            tier = "A" if d2911a is None else ("B" if d2911b is None else "X")
            rank = (
                (100 if tier == "A" else 65 if tier == "B" else 0)
                + cons * 30
                + weak * 25
                + cal * 15
                + emp * 10
                + p2 * 20
                - fts * 25
                - role_fts * 20
                - max_zero * 25
                - p00 * 40
                + min(total_lambda, 3.5) * 2.0
                + min(weak_lambda, 1.8) * 2.0
            )
            rows.append((rank, tier, fixture, prediction, weak, cal, cons, emp, max_zero, fts, role_fts, l5s, role_l5s, l5b, total_lambda, p2, p00, weak_lambda, d299a, d299b, d2910a, d2910b, d2911a, d2911b, best))

        rows.sort(key=lambda x: x[0], reverse=True)
        self.stdout.write("\nRANKED")
        for rank, tier, fixture, prediction, weak, cal, cons, emp, max_zero, fts, role_fts, l5s, role_l5s, l5b, total_lambda, p2, p00, weak_lambda, d299a, d299b, d2910a, d2910b, d2911a, d2911b, match in rows:
            self.stdout.write(
                f"{tier} | {fixture.home_team.name} vs {fixture.away_team.name} | rank={rank:.2f} "
                f"raw={float(prediction.score or 0):.1f} prob={float(prediction.probability or 0):.3f} "
                f"weak={weak:.3f} cal={cal:.3f} cons={cons:.3f} emp={emp:.3f} maxZero={max_zero:.3f} "
                f"goalLambda={total_lambda:.2f} P2plus={p2:.3f} P00={p00:.3f} weakGoalLambda={weak_lambda:.2f} "
                f"maxOverallFTS={fts:.3f} maxRoleFTS={role_fts:.3f} minL5scored={l5s}/5 "
                f"minRoleL5scored={role_l5s}/5 minL5BTTS={l5b}/5 "
                f"v299A={decision_text(d299a)} v299B={decision_text(d299b)} "
                f"v2910A={decision_text(d2910a)} v2910B={decision_text(d2910b)} "
                f"v2911A={decision_text(d2911a)} v2911B={decision_text(d2911b)} match={match:.2f}"
            )

        a_count = sum(1 for row in rows if row[1] == "A")
        b_count = sum(1 for row in rows if row[1] == "B")
        x_count = sum(1 for row in rows if row[1] == "X")
        self.stdout.write(
            f"\nSUMMARY | targets={len(targets)} matched={len(matched_targets)} missing={missing} excluded={excluded} "
            f"no_pred={no_pred} score_errors={score_errors} ranked={len(rows)} A={a_count} B={b_count} X={x_count}"
        )
