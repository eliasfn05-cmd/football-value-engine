from __future__ import annotations

import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from engine.btts_v25_policy import anti_zero_metrics
from engine.btts_v291_policy import tier_a_decision_v291
from engine.btts_v299_policy import tier_a_decision_v299, anti_zero_decision_v299
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
    help = "Reusable BTTS screenshot/target scanner using V2.9.1 + V2.9.9 with odds excluded."

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
        self.stdout.write(f"POLICY | V2.9.1 + V2.9.9 | odds excluded | match_threshold={threshold:.2f}")

        # Phase 1: match all targets first. Do not score one-by-one; that repeatedly rebuilds
        # features and causes O(targets * DB/API work) latency on large screenshot batches.
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

        # Phase 2: determine which matched fixtures actually need V8 BTTS predictions.
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
            # Repair the stale-state edge case: a fixture can have an unchanged fingerprint
            # even when its BTTS Prediction row is absent. Clearing only those states forces
            # the batch scorer to rebuild the missing predictions without forcing the whole day.
            FixtureScoreState.objects.filter(
                fixture_id__in=missing_pred_fixture_ids,
                model_version=V8_MODEL_VERSION,
            ).delete()
            try:
                call_command("score_v8", date=day, summary_only=True)
            except Exception as exc:
                score_errors += 1
                self.stdout.write(f"BATCH_SCORE_ERROR | {exc.__class__.__name__}: {exc}")

        # Phase 3: load all latest BTTS predictions in one query and rank.
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
            d291 = tier_a_decision_v291(prediction)
            d299a = tier_a_decision_v299(prediction)
            d299b = anti_zero_decision_v299(prediction)
            available = bool(metrics.get("available"))
            weak = float(metrics.get("weakest_score_probability", 0.0) or 0.0)
            cal = float(metrics.get("calibrated_probability", 0.0) or 0.0)
            cons = float(metrics.get("consensus_probability", 0.0) or 0.0)
            emp = float(metrics.get("empirical_btts", 0.0) or 0.0)
            home_o = metrics.get("home_overall") or {}
            away_o = metrics.get("away_overall") or {}
            fts = max(float(home_o.get("failed_to_score_rate", 1.0) or 0.0), float(away_o.get("failed_to_score_rate", 1.0) or 0.0)) if available else 1.0
            l5s = min(int(home_o.get("last5_scored", 0) or 0), int(away_o.get("last5_scored", 0) or 0)) if available else 0

            tier = "A" if d291 is None and d299a is None else ("B" if d299b is None else "X")
            rank = (100 if tier == "A" else 65 if tier == "B" else 0) + cons * 35 + weak * 30 + cal * 20 + emp * 15 - fts * 20
            rows.append((rank, tier, fixture, prediction, weak, cal, cons, emp, fts, l5s, d291, d299a, d299b, best))

        rows.sort(key=lambda x: x[0], reverse=True)
        self.stdout.write("\nRANKED")
        for rank, tier, fixture, prediction, weak, cal, cons, emp, fts, l5s, d291, d299a, d299b, match in rows:
            self.stdout.write(
                f"{tier} | {fixture.home_team.name} vs {fixture.away_team.name} | rank={rank:.2f} "
                f"raw={float(prediction.score or 0):.1f} prob={float(prediction.probability or 0):.3f} "
                f"weak={weak:.3f} cal={cal:.3f} cons={cons:.3f} emp={emp:.3f} maxFTS={fts:.3f} "
                f"minL5scored={l5s}/5 v291={decision_text(d291)} v299A={decision_text(d299a)} "
                f"v299B={decision_text(d299b)} match={match:.2f}"
            )

        a_count = sum(1 for row in rows if row[1] == "A")
        b_count = sum(1 for row in rows if row[1] == "B")
        x_count = sum(1 for row in rows if row[1] == "X")
        self.stdout.write(
            f"\nSUMMARY | targets={len(targets)} matched={len(matched_targets)} missing={missing} excluded={excluded} "
            f"no_pred={no_pred} score_errors={score_errors} ranked={len(rows)} A={a_count} B={b_count} X={x_count}"
        )
