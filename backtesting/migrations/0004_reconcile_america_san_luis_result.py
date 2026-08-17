from decimal import Decimal

from django.db import migrations
from django.utils import timezone


def reconcile_america_san_luis(apps, schema_editor):
    Ledger = apps.get_model("engine", "PremiumPublicationLedger")
    Outcome = apps.get_model("backtesting", "PredictionOutcome")
    Fixture = apps.get_model("engine", "Fixture")

    ledger = (
        Ledger.objects.select_related(
            "prediction",
            "prediction__fixture",
            "prediction__fixture__home_team",
            "prediction__fixture__away_team",
        )
        .filter(
            target_date="2026-08-16",
            market="OVER_2_5",
            prediction__fixture__home_team__name__icontains="America",
            prediction__fixture__away_team__name__icontains="San Luis",
        )
        .order_by("-published_at")
        .first()
    )
    if ledger is None:
        return

    fixture = ledger.prediction.fixture
    Fixture.objects.filter(pk=fixture.pk).update(
        home_goals=3,
        away_goals=0,
        status="FT",
    )

    odds = Decimal(str(ledger.odds or "1.75"))
    stake = Decimal("1.000")
    Outcome.objects.update_or_create(
        prediction_id=ledger.prediction_id,
        defaults={
            "result": "WIN",
            "home_goals": 3,
            "away_goals": 0,
            "stake_units": stake,
            "profit_units": (odds - Decimal("1.000")) * stake,
            "settled_at": timezone.now(),
            "settlement_reason": "reconciled_final_score_2026-08-16",
        },
    )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("backtesting", "0003_register_america_san_luis_premium_win"),
    ]

    operations = [
        migrations.RunPython(reconcile_america_san_luis, noop_reverse),
    ]
