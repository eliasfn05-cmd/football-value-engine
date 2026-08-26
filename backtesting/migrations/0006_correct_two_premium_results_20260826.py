from decimal import Decimal

from django.db import migrations
from django.db.models import Q
from django.utils import timezone


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


def _find_ledger(Ledger, item):
    qs = Ledger.objects.select_related(
        "prediction",
        "prediction__fixture",
        "prediction__fixture__home_team",
        "prediction__fixture__away_team",
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
            Q(market__iexact="OVER_2_5")
            | Q(market__icontains="2.5")
            | Q(prediction__market__iexact="OVER_2_5")
            | Q(prediction__market__icontains="2.5")
        )

    return qs.order_by("-published_at").first()


def correct_two_premium_results(apps, schema_editor):
    Ledger = apps.get_model("engine", "PremiumPublicationLedger")
    Outcome = apps.get_model("backtesting", "PredictionOutcome")
    Fixture = apps.get_model("scanner", "Fixture")

    for item in CORRECTIONS:
        ledger = _find_ledger(Ledger, item)
        if ledger is None:
            continue

        prediction = ledger.prediction
        odds_value = ledger.odds or getattr(prediction, "market_odds", None) or 0
        odds = Decimal(str(odds_value))
        stake = Decimal("1.000")
        profit = (odds - Decimal("1.000")) * stake if odds > 0 else Decimal("0")

        Outcome.objects.update_or_create(
            prediction_id=prediction.id,
            defaults={
                "result": "WIN",
                "home_goals": item["home_goals"],
                "away_goals": item["away_goals"],
                "stake_units": stake,
                "profit_units": profit,
                "settled_at": timezone.now(),
                "settlement_reason": "manual_verified_result_correction_20260826",
            },
        )

        Fixture.objects.filter(pk=prediction.fixture_id).update(
            home_goals=item["home_goals"],
            away_goals=item["away_goals"],
            status="finished",
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("backtesting", "0005_reconcile_official_premium_20260817"),
    ]

    operations = [
        migrations.RunPython(correct_two_premium_results, noop_reverse),
    ]
