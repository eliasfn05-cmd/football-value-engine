from decimal import Decimal

from django.db import migrations
from django.db.models import Q
from django.utils import timezone


# User-verified 90-minute scores for the 2026-08-27 BTTS block.
# Football betting settlement uses regulation-time score; extra time is not
# included for the Qarabag-Twente BTTS result.
VERIFIED_RESULTS = [
    {"home": "St. Gallen", "away": "Nordsjaelland", "home_goals": 2, "away_goals": 3, "result": "WIN"},
    {"home": "Rijeka", "away": "Midtjylland", "home_goals": 1, "away_goals": 4, "result": "WIN"},
    {"home": "Ferencvaros", "away": "Trabzonspor", "home_goals": 4, "away_goals": 0, "result": "LOSS"},
    {"home": "Qarabag", "away": "Twente", "home_goals": 1, "away_goals": 2, "result": "WIN"},
]


def _find_ledger(Ledger, item):
    return (
        Ledger.objects.select_related(
            "prediction",
            "prediction__fixture",
            "prediction__fixture__home_team",
            "prediction__fixture__away_team",
        )
        .filter(
            Q(prediction__fixture__home_team__name__icontains=item["home"])
            | Q(prediction__fixture__away_team__name__icontains=item["home"]),
            Q(prediction__fixture__home_team__name__icontains=item["away"])
            | Q(prediction__fixture__away_team__name__icontains=item["away"]),
            Q(market__iexact="BTTS") | Q(prediction__market__iexact="BTTS"),
        )
        .order_by("-published_at")
        .first()
    )


def settle_verified_block(apps, schema_editor):
    Ledger = apps.get_model("engine", "PremiumPublicationLedger")
    Outcome = apps.get_model("backtesting", "PredictionOutcome")

    for item in VERIFIED_RESULTS:
        ledger = _find_ledger(Ledger, item)
        if ledger is None:
            # History remains restricted to officially published Premium/Tier
            # selections represented by the publication ledger.
            continue

        prediction = ledger.prediction
        odds_value = ledger.odds or getattr(prediction, "market_odds", None) or 0
        odds = Decimal(str(odds_value))
        stake = Decimal("1.000")
        if item["result"] == "WIN":
            profit = (odds - Decimal("1.000")) * stake if odds > 0 else Decimal("0")
        else:
            profit = -stake

        Outcome.objects.update_or_create(
            prediction_id=prediction.id,
            defaults={
                "result": item["result"],
                "home_goals": item["home_goals"],
                "away_goals": item["away_goals"],
                "stake_units": stake,
                "profit_units": profit,
                "settled_at": timezone.now(),
                "settlement_reason": "manual_verified_btts_block_20260827_90min",
            },
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("backtesting", "0007_register_three_btts_wins_20260827"),
    ]

    operations = [
        migrations.RunPython(settle_verified_block, noop_reverse),
    ]
