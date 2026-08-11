from decimal import Decimal

from django.db import migrations
from django.utils import timezone


def apply_sirius_score(apps, schema_editor):
    PremiumPublicationLedger = apps.get_model("engine", "PremiumPublicationLedger")
    PredictionOutcome = apps.get_model("backtesting", "PredictionOutcome")

    ledgers = PremiumPublicationLedger.objects.select_related(
        "prediction",
        "prediction__fixture",
        "prediction__fixture__home_team",
        "prediction__fixture__away_team",
    ).filter(target_date="2026-08-10", market="BTTS", selection="YES")

    for ledger in ledgers:
        fixture = ledger.prediction.fixture
        home = (fixture.home_team.name or "").strip().lower()
        away = (fixture.away_team.name or "").strip().lower()
        if "sirius" not in home or "brommapojkarna" not in away:
            continue

        fixture.home_goals = 2
        fixture.away_goals = 2
        fixture.status = "FT"
        fixture.save(update_fields=["home_goals", "away_goals", "status"])

        PredictionOutcome.objects.update_or_create(
            prediction_id=ledger.prediction_id,
            defaults={
                "result": "WIN",
                "home_goals": 2,
                "away_goals": 2,
                "stake_units": Decimal("1.000"),
                "profit_units": Decimal("0.6200"),
                "settled_at": timezone.now(),
                "settlement_reason": "btts_yes",
            },
        )


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("engine", "0005_premium_publication_ledger"),
        ("backtesting", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(apply_sirius_score, reverse_noop),
    ]
