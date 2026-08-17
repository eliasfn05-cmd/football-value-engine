from decimal import Decimal

from django.db import migrations
from django.utils import timezone


TARGET_DATE = "2026-08-17"


def _find_ledger(Ledger, home, away, market):
    return (
        Ledger.objects.select_related(
            "prediction",
            "prediction__fixture",
            "prediction__fixture__home_team",
            "prediction__fixture__away_team",
        )
        .filter(
            target_date=TARGET_DATE,
            market=market,
            prediction__fixture__home_team__name__iexact=home,
            prediction__fixture__away_team__name__iexact=away,
        )
        .order_by("published_at")
        .first()
    )


def reconcile_official_premium_20260817(apps, schema_editor):
    Ledger = apps.get_model("engine", "PremiumPublicationLedger")
    Daily = apps.get_model("engine", "DailyPremiumSelection")
    Outcome = apps.get_model("backtesting", "PredictionOutcome")

    # Deportivo La Coruna vs Elche was not part of the first official Premium
    # publication for this date. Remove it from the Premium source-of-truth so
    # it cannot contaminate history/ROI. The underlying prediction is retained.
    rogue = (
        Ledger.objects.filter(
            target_date=TARGET_DATE,
            prediction__fixture__home_team__name__icontains="Coruna",
            prediction__fixture__away_team__name__iexact="Elche",
        )
        .select_related("prediction")
        .first()
    )
    if rogue is not None:
        Outcome.objects.filter(prediction_id=rogue.prediction_id).delete()
        Daily.objects.filter(target_date=TARGET_DATE, prediction_id=rogue.prediction_id).delete()
        rogue.delete()

    # User-confirmed official summary. Scores are intentionally not overwritten:
    # settlement is reconciled from the confirmed WIN/LOSS status only.
    official = [
        ("Polessya", "Zorya Luhansk", "BTTS", "WIN"),
        ("Novi Pazar", "Vojvodina", "OVER_2_5", "LOSS"),
        ("Al Anwar", "Al-Ahli Jeddah", "BTTS", "WIN"),
    ]

    for home, away, market, result in official:
        ledger = _find_ledger(Ledger, home, away, market)
        if ledger is None:
            # Tolerate harmless provider naming variants while still restricting
            # reconciliation to the immutable Premium ledger for this date.
            ledger = (
                Ledger.objects.select_related("prediction", "prediction__fixture")
                .filter(
                    target_date=TARGET_DATE,
                    market=market,
                    prediction__fixture__home_team__name__icontains=home.split()[0],
                    prediction__fixture__away_team__name__icontains=away.split()[0],
                )
                .order_by("published_at")
                .first()
            )
        if ledger is None:
            continue

        stake = Decimal("1.000")
        odds = Decimal(str(ledger.odds))
        profit = (odds - Decimal("1.000")) * stake if result == "WIN" else -stake
        Outcome.objects.update_or_create(
            prediction_id=ledger.prediction_id,
            defaults={
                "result": result,
                "stake_units": stake,
                "profit_units": profit,
                "settled_at": timezone.now(),
                "settlement_reason": "official_premium_reconciliation_2026-08-17",
            },
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("backtesting", "0004_reconcile_america_san_luis_result"),
    ]

    operations = [
        migrations.RunPython(reconcile_official_premium_20260817, noop_reverse),
    ]
