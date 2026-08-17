from decimal import Decimal

from django.db import migrations
from django.utils import timezone


def register_confirmed_win(apps, schema_editor):
    Ledger = apps.get_model("engine", "PremiumPublicationLedger")
    Outcome = apps.get_model("backtesting", "PredictionOutcome")

    ledger = (
        Ledger.objects.select_related(
            "prediction",
            "prediction__fixture",
            "prediction__fixture__home_team",
            "prediction__fixture__away_team",
        )
        .filter(
            target_date="2026-08-16",
            model_version="score_v8",
            market="OVER_2_5",
            selection="OVER",
            prediction__fixture__home_team__name__icontains="America",
            prediction__fixture__away_team__name__icontains="Atletico San Luis",
        )
        .order_by("-published_at")
        .first()
    )
    if ledger is None:
        return

    odds = Decimal(str(ledger.odds or "1.75"))
    stake = Decimal("1.000")
    profit = (odds - Decimal("1.000")) * stake

    Outcome.objects.update_or_create(
        prediction_id=ledger.prediction_id,
        defaults={
            "result": "WIN",
            "stake_units": stake,
            "profit_units": profit,
            "settled_at": timezone.now(),
            "settlement_reason": "confirmed_by_operator_2026-08-16",
        },
    )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("backtesting", "0002_sprint791_bootstrap_sirius_premium"),
    ]

    operations = [
        migrations.RunPython(register_confirmed_win, noop_reverse),
    ]
