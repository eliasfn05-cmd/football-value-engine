from datetime import date, datetime, time, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from backtesting.premium_settlement import settle_published_premium
from engine.models import Prediction
from engine.premium_replacement import PremiumReplacementService
from engine.premium_selection import DailyPremiumSelector
from engine.score_v8 import V8_MODEL_VERSION


class Command(BaseCommand):
    help = "Rank, persist and automatically replace up to three operational Premium picks for a date."

    def add_arguments(self, parser):
        parser.add_argument("--date", dest="target_date", required=True, help="YYYY-MM-DD")
        parser.add_argument("--max-picks", type=int, default=3)

    def handle(self, *args, **options):
        try:
            target_date = date.fromisoformat(options["target_date"])
        except ValueError as exc:
            raise CommandError("--date must use YYYY-MM-DD") from exc

        ledger = settle_published_premium(model_version=V8_MODEL_VERSION)
        self.stdout.write(
            f"[premium-ledger] settled={ledger.get('settled', 0)} wins={ledger.get('wins', 0)} "
            f"losses={ledger.get('losses', 0)} voids={ledger.get('voids', 0)}"
        )

        replacement = PremiumReplacementService(max_picks=options["max_picks"])
        rows = replacement.reconcile(target_date, trigger="select_premium")
        selector = replacement.selector

        if not rows:
            self.stdout.write("[premium] NO BET: no prediction cleared Sprint 7.9.5 professional value gates")
            start = timezone.make_aware(datetime.combine(target_date, time.min))
            end = start + timedelta(days=1)
            future_start = max(start, timezone.now())
            near = (
                Prediction.objects.select_related(
                    "fixture", "fixture__home_team", "fixture__away_team", "fixture__competition_ref"
                )
                .filter(
                    model_version=V8_MODEL_VERSION,
                    fixture__kickoff__gte=future_start,
                    fixture__kickoff__lt=end,
                    market_odds__isnull=False,
                    expected_value__gt=0,
                )
                .order_by("-score", "-expected_value")[:10]
            )
            for prediction in near:
                calibration = selector.calibrator.calibrate(prediction)
                reject = selector.rejection_reasons(prediction)
                fixture = prediction.fixture
                self.stdout.write(
                    f"[premium-audit] {fixture.home_team.name} vs {fixture.away_team.name} "
                    f"status={fixture.status} market={prediction.market} raw_p={calibration.raw_probability:.3f} "
                    f"cal_p={calibration.calibrated_probability:.3f} odds={prediction.market_odds} "
                    f"cal_edge={calibration.calibrated_edge:.3f} reliable_ev={calibration.reliable_ev:.3f} "
                    f"reliability={calibration.reliability:.3f} score={float(prediction.score or 0):.1f} "
                    f"reject={','.join(reject) if reject else 'tier/rank_only'}"
                )
            return

        for row in rows:
            prediction = row.prediction
            fixture = prediction.fixture
            calibration = selector.calibrator.calibrate(prediction)
            promotion_reason = (row.rationale or {}).get("promotion_reason")
            promotion_suffix = f" promotion={promotion_reason}" if promotion_reason else ""
            self.stdout.write(
                f"[premium] #{row.rank} tier={row.premium_tier} rank_score={row.premium_rank_score} "
                f"{fixture.home_team.name} vs {fixture.away_team.name} "
                f"status={fixture.status} market={prediction.market} raw_p={calibration.raw_probability:.3f} "
                f"cal_p={calibration.calibrated_probability:.3f} odds={prediction.market_odds} "
                f"cal_edge={calibration.calibrated_edge:.3f} reliable_ev={calibration.reliable_ev:.3f} "
                f"reliability={calibration.reliability:.3f} score={prediction.score}{promotion_suffix}"
            )
        self.stdout.write(self.style.SUCCESS(f"[premium] selected={len(rows)} ledgered={len(rows)} auto_replacement=enabled"))
