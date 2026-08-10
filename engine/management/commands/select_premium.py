from datetime import date, datetime, time, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from backtesting.services import SettlementService
from engine.models import Prediction, PremiumPublicationLedger
from engine.premium_selection import DailyPremiumSelector
from engine.score_v8 import V8_MODEL_VERSION


class Command(BaseCommand):
    help = "Rank and persist up to three operational Premium picks for a date."

    def add_arguments(self, parser):
        parser.add_argument("--date", dest="target_date", required=True, help="YYYY-MM-DD")
        parser.add_argument("--max-picks", type=int, default=3)

    @staticmethod
    def _snapshot(row, calibration):
        prediction = row.prediction
        fixture = prediction.fixture
        return {
            "fixture_id": fixture.id,
            "fixture_external_id": fixture.external_id,
            "home_team": fixture.home_team.name,
            "away_team": fixture.away_team.name,
            "kickoff": fixture.kickoff.isoformat(),
            "market": prediction.market,
            "selection": prediction.selection,
            "odds": float(prediction.market_odds),
            "raw_probability": calibration.raw_probability,
            "calibrated_probability": calibration.calibrated_probability,
            "implied_probability": calibration.implied_probability,
            "calibrated_edge": calibration.calibrated_edge,
            "reliable_ev": calibration.reliable_ev,
            "reliability": calibration.reliability,
            "score": float(prediction.score),
            "premium_tier": row.premium_tier,
            "premium_rank_score": float(row.premium_rank_score),
            "rationale": row.rationale or {},
        }

    def handle(self, *args, **options):
        try:
            target_date = date.fromisoformat(options["target_date"])
        except ValueError as exc:
            raise CommandError("--date must use YYYY-MM-DD") from exc

        # Settle anything already published before refreshing the current Top.
        ledger = SettlementService().settle_finished_premium(model_version=V8_MODEL_VERSION)
        self.stdout.write(
            f"[premium-ledger] settled={ledger.get('settled', 0)} wins={ledger.get('wins', 0)} "
            f"losses={ledger.get('losses', 0)} voids={ledger.get('voids', 0)}"
        )

        selector = DailyPremiumSelector(max_picks=options["max_picks"])
        rows = selector.select(target_date)
        if not rows:
            self.stdout.write("[premium] NO BET: no prediction cleared Sprint 7.9 professional value gates")
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
                    f"market={prediction.market} raw_p={calibration.raw_probability:.3f} "
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

            # Immutable publication proof. DailyPremiumSelection may be rebuilt
            # later, but this ledger row survives and remains settlement truth.
            PremiumPublicationLedger.objects.get_or_create(
                prediction=prediction,
                defaults={
                    "target_date": target_date,
                    "published_rank": row.rank,
                    "premium_tier": row.premium_tier,
                    "premium_rank_score": row.premium_rank_score,
                    "model_version": row.model_version,
                    "market": prediction.market,
                    "selection": prediction.selection,
                    "odds": prediction.market_odds,
                    "snapshot": self._snapshot(row, calibration),
                },
            )

            self.stdout.write(
                f"[premium] #{row.rank} tier={row.premium_tier} rank_score={row.premium_rank_score} "
                f"{fixture.home_team.name} vs {fixture.away_team.name} "
                f"market={prediction.market} raw_p={calibration.raw_probability:.3f} "
                f"cal_p={calibration.calibrated_probability:.3f} odds={prediction.market_odds} "
                f"cal_edge={calibration.calibrated_edge:.3f} reliable_ev={calibration.reliable_ev:.3f} "
                f"reliability={calibration.reliability:.3f} score={prediction.score}"
            )
        self.stdout.write(self.style.SUCCESS(f"[premium] selected={len(rows)} ledgered={len(rows)}"))