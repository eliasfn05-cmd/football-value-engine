from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from backtesting.models import PredictionOutcome
from engine.models import PremiumPublicationLedger


CORRECTIONS = [
    {
        "home": "Sportivo Ameliano",
        "away": "Deportivo Recoleta",
        "market": "BTTS",
        "home_goals": 3,
        "away_goals": 1,
    },
    {
        "home": "Heart Of Midlothian",
        "away": "Rapid Vienna",
        "market": "OVER_2_5",
        "home_goals": 2,
        "away_goals": 2,
    },
]


class Command(BaseCommand):
    help = "Correct known Premium historical settlement mistakes reported on 2026-08-26."

    def _find_ledger(self, item):
        qs = PremiumPublicationLedger.objects.select_related(
            "prediction__fixture__home_team", "prediction__fixture__away_team"
        ).filter(
            Q(prediction__fixture__home_team__name__icontains=item["home"])
            | Q(prediction__fixture__away_team__name__icontains=item["home"]),
            Q(prediction__fixture__home_team__name__icontains=item["away"])
            | Q(prediction__fixture__away_team__name__icontains=item["away"]),
        )
        if item["market"] == "BTTS":
            qs = qs.filter(Q(market__iexact="BTTS") | Q(prediction__market__iexact="BTTS"))
        else:
            qs = qs.filter(
                Q(market__iexact="OVER_2_5") | Q(market__icontains="2.5")
                | Q(prediction__market__iexact="OVER_2_5") | Q(prediction__market__icontains="2.5")
            )
        return qs.order_by("-published_at").first()

    @transaction.atomic
    def handle(self, *args, **options):
        for item in CORRECTIONS:
            ledger = self._find_ledger(item)
            if ledger is None:
                raise CommandError(f"Premium publication not found: {item['home']} vs {item['away']}")

            prediction = ledger.prediction
            odds = Decimal(str(ledger.odds or prediction.market_odds or 0))
            stake = Decimal("1.000")
            profit = (odds - Decimal("1")) * stake if odds > 0 else Decimal("0")

            outcome, _ = PredictionOutcome.objects.get_or_create(prediction=prediction)
            outcome.result = PredictionOutcome.RESULT_WIN
            outcome.home_goals = item["home_goals"]
            outcome.away_goals = item["away_goals"]
            outcome.stake_units = stake
            outcome.profit_units = profit
            outcome.settled_at = timezone.now()
            outcome.settlement_reason = "manual_verified_result_correction_20260826"
            outcome.save()

            fixture = prediction.fixture
            fixture.home_goals = item["home_goals"]
            fixture.away_goals = item["away_goals"]
            fixture.status = "finished"
            fixture.save(update_fields=["home_goals", "away_goals", "status"])

            self.stdout.write(self.style.SUCCESS(
                f"CORRECTED WIN: {fixture.home_team} {item['home_goals']}-{item['away_goals']} "
                f"{fixture.away_team} | {ledger.market} | prediction={prediction.id}"
            ))
