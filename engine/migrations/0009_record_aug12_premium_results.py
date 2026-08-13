from decimal import Decimal

from django.db import migrations
from django.utils import timezone


def record_aug12_results(apps, schema_editor):
    PremiumPublicationLedger = apps.get_model("engine", "PremiumPublicationLedger")
    PredictionOutcome = apps.get_model("backtesting", "PredictionOutcome")

    cases = [
        {
            "home_contains": "bremer",
            "away_contains": "phonix",
            "home_goals": 0,
            "away_goals": 0,
        },
        {
            "home_contains": "magallanes",
            "away_contains": "cobreloa",
            "home_goals": 2,
            "away_goals": 2,
        },
        {
            "home_contains": "tampa bay",
            "away_contains": "louisville",
            "home_goals": 0,
            "away_goals": 0,
        },
    ]

    ledgers = PremiumPublicationLedger.objects.select_related(
        "prediction",
        "prediction__fixture",
        "prediction__fixture__home_team",
        "prediction__fixture__away_team",
    ).filter(target_date="2026-08-12", market="OVER_2_5")

    for case in cases:
        for ledger in ledgers:
            fixture = ledger.prediction.fixture
            home = (fixture.home_team.name or "").strip().lower()
            away = (fixture.away_team.name or "").strip().lower()
            if case["home_contains"] not in home or case["away_contains"] not in away:
                continue

            hg = case["home_goals"]
            ag = case["away_goals"]
            fixture.home_goals = hg
            fixture.away_goals = ag
            fixture.status = "FT"
            fixture.save(update_fields=["home_goals", "away_goals", "status"])

            won = (hg + ag) >= 3
            odds = Decimal(str(ledger.odds or "1.00"))
            profit = (odds - Decimal("1.0000")) if won else Decimal("-1.0000")
            PredictionOutcome.objects.update_or_create(
                prediction_id=ledger.prediction_id,
                defaults={
                    "result": "WIN" if won else "LOSS",
                    "home_goals": hg,
                    "away_goals": ag,
                    "stake_units": Decimal("1.000"),
                    "profit_units": profit,
                    "settled_at": timezone.now(),
                    "settlement_reason": "over_2_5_backtest_aug12",
                },
            )
            break


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("engine", "0008_record_aug11_premium_results"),
        ("backtesting", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(record_aug12_results, reverse_noop),
    ]
