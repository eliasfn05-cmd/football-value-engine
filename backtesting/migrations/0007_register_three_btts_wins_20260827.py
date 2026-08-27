from decimal import Decimal

from django.db import migrations
from django.db.models import Q
from django.utils import timezone


VERIFIED_WINS = [
    {"home": "St. Gallen", "away": "Nordsjaelland"},
    {"home": "Rijeka", "away": "Midtjylland"},
    {"home": "Qarabag", "away": "Twente"},
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


def register_verified_btts_wins(apps, schema_editor):
    Ledger = apps.get_model("engine", "PremiumPublicationLedger")
    Outcome = apps.get_model("backtesting", "PredictionOutcome")

    for item in VERIFIED_WINS:
        ledger = _find_ledger(Ledger, item)
        if ledger is None:
            continue

        prediction = ledger.prediction
        odds_value = ledger.odds or getattr(prediction, "market_odds", None) or 0
        odds = Decimal(str(odds_value))
        stake = Decimal("1.000")
        profit = (odds - Decimal("1.000")) * stake if odds > 0 else Decimal("0")

        existing = Outcome.objects.filter(prediction_id=prediction.id).first()
        home_goals = existing.home_goals if existing else None
        away_goals = existing.away_goals if existing else None

        Outcome.objects.update_or_create(
            prediction_id=prediction.id,
            defaults={
                "result": "WIN",
                "home_goals": home_goals,
                "away_goals": away_goals,
                "stake_units": stake,
                "profit_units": profit,
                "settled_at": timezone.now(),
                "settlement_reason": "manual_verified_btts_win_20260827",
            },
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("backtesting", "0006_correct_two_premium_results_20260826"),
    ]

    operations = [
        migrations.RunPython(register_verified_btts_wins, noop_reverse),
    ]
