from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from backtesting.models import BttsV293HoldoutSnapshot
from engine.btts_v25_policy import anti_zero_metrics
from engine.btts_v291_policy import tier_a_decision_v291
from engine.models import Prediction

VERSION = "V2.9.3-FROZEN"


def f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def blocked(decision):
    return bool(decision and getattr(decision, "blocked", False))


def v293_score(prediction, metrics):
    raw = f(prediction.score)
    emp = f(metrics.get("empirical_btts"))
    cons = f(metrics.get("consensus_probability"))
    cal = f(metrics.get("calibrated_probability"))
    weak = f(metrics.get("weakest_score_probability"))
    score = 100.0 * (.35 * emp + .25 * cons + .20 * cal + .20 * weak)
    if raw >= 85 and emp < .68:
        score -= 10
    if raw >= 85 and cal < .72:
        score -= 4
    if raw >= 90 and cons < .73:
        score -= 4
    if emp >= .80:
        score += 3
    if cons >= .75:
        score += 2
    if weak >= .80:
        score += 2
    return score


class Command(BaseCommand):
    help = "Capture immutable pre-kickoff A#1 for frozen BTTS V2.9.3 challenger."

    def add_arguments(self, parser):
        parser.add_argument("--date", dest="target_date", help="YYYY-MM-DD; default today")

    def handle(self, *args, **options):
        raw_date = options.get("target_date")
        try:
            target = date.fromisoformat(raw_date) if raw_date else timezone.localdate()
        except ValueError as exc:
            raise CommandError("--date must be YYYY-MM-DD") from exc

        if target < timezone.localdate():
            raise CommandError("Holdout capture refuses past dates: no retrospective snapshots allowed.")

        existing = BttsV293HoldoutSnapshot.objects.filter(target_date=target, challenger_version=VERSION, rank=1).first()
        if existing:
            self.stdout.write(self.style.WARNING(
                f"ALREADY CAPTURED {target} | A#1 prediction={existing.prediction_id} fixture={existing.fixture_id}; immutable, no overwrite."
            ))
            return

        qs = Prediction.objects.filter(
            market__iexact="BTTS",
            fixture__kickoff__date=target,
        ).select_related("fixture", "fixture__home_team", "fixture__away_team").order_by("-created_at")

        by_fixture = defaultdict(list)
        for prediction in qs:
            by_fixture[prediction.fixture_id].append(prediction)

        candidates = []
        now = timezone.now()
        for predictions in by_fixture.values():
            prediction = predictions[0]
            if prediction.fixture.kickoff <= now:
                continue
            if blocked(tier_a_decision_v291(prediction)):
                continue
            metrics = anti_zero_metrics(prediction)
            if not metrics.get("available"):
                continue
            score = v293_score(prediction, metrics)
            candidates.append((score, f(prediction.score), prediction, metrics))

        if not candidates:
            self.stdout.write(self.style.WARNING(f"NO CAPTURE {target}: no pre-kickoff V2.9.1 Tier A candidate available for frozen V2.9.3."))
            return

        score, raw, prediction, metrics = max(candidates, key=lambda row: (row[0], row[1], row[2].id))
        fixture = prediction.fixture
        snapshot = {
            "captured_pre_kickoff": True,
            "formula": "V2.9.3 frozen 35emp/25cons/20cal/20weak + fixed penalties/bonuses",
            "fixture_external_id": fixture.external_id,
            "home": fixture.home_team.name,
            "away": fixture.away_team.name,
            "kickoff": fixture.kickoff.isoformat(),
            "prediction_model_version": prediction.model_version,
            "prediction_tier": prediction.tier,
            "prediction_probability": f(prediction.probability),
            "edge": f(prediction.edge),
            "expected_value": f(prediction.expected_value),
        }
        row = BttsV293HoldoutSnapshot.objects.create(
            target_date=target,
            rank=1,
            challenger_version=VERSION,
            fixture=fixture,
            prediction=prediction,
            recalibrated_score=Decimal(str(round(score, 5))),
            raw_score=Decimal(str(round(raw, 5))),
            market_odds=Decimal(str(prediction.market_odds)) if prediction.market_odds is not None else None,
            empirical_btts=Decimal(str(round(f(metrics.get("empirical_btts")), 6))),
            consensus_probability=Decimal(str(round(f(metrics.get("consensus_probability")), 6))),
            calibrated_probability=Decimal(str(round(f(metrics.get("calibrated_probability")), 6))),
            weakest_probability=Decimal(str(round(f(metrics.get("weakest_score_probability")), 6))),
            snapshot=snapshot,
        )
        self.stdout.write(self.style.SUCCESS(
            f"CAPTURED {target} | {VERSION} A#1 | holdout_id={row.id} prediction={prediction.id} fixture={fixture.id} | "
            f"{fixture.home_team.name} vs {fixture.away_team.name} | v293={score:.2f} raw={raw:.2f} odds={prediction.market_odds}"
        ))
