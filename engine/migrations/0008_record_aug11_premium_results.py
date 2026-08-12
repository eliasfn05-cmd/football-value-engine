from decimal import Decimal

from django.db import migrations
from django.utils import timezone


def _record(apps, *, home_contains, away_contains, market, home_goals, away_goals, result, odds=None, rank=1, tier="B"):
    Prediction = apps.get_model("engine", "Prediction")
    PremiumPublicationLedger = apps.get_model("engine", "PremiumPublicationLedger")
    PredictionOutcome = apps.get_model("backtesting", "PredictionOutcome")

    qs = Prediction.objects.select_related("fixture", "fixture__home_team", "fixture__away_team").filter(
        fixture__kickoff__date="2026-08-11",
        fixture__home_team__name__icontains=home_contains,
        fixture__away_team__name__icontains=away_contains,
        market=market,
    ).order_by("-created_at")
    prediction = qs.first()
    if prediction is None:
        return

    fixture = prediction.fixture
    fixture.home_goals = home_goals
    fixture.away_goals = away_goals
    fixture.status = "FT"
    fixture.save(update_fields=["home_goals", "away_goals", "status"])

    published_odds = prediction.market_odds
    if odds is not None:
        published_odds = Decimal(str(odds))
    if published_odds is None:
        return

    ledger, _ = PremiumPublicationLedger.objects.get_or_create(
        prediction_id=prediction.id,
        defaults={
            "target_date": "2026-08-11",
            "published_rank": rank,
            "premium_tier": tier,
            "premium_rank_score": prediction.score,
            "model_version": prediction.model_version,
            "market": prediction.market,
            "selection": prediction.selection,
            "odds": published_odds,
            "snapshot": {
                "recovered_by": "sprint7.10_aug11_backtest",
                "fixture_id": fixture.id,
                "home_team": fixture.home_team.name,
                "away_team": fixture.away_team.name,
                "market": prediction.market,
                "selection": prediction.selection,
                "odds": float(published_odds),
                "score": float(prediction.score or 0),
            },
        },
    )

    # Preserve the immutable publication odds if a ledger already existed.
    settled_odds = ledger.odds
    profit = Decimal("-1.0000")
    if result == "WIN":
        profit = (Decimal(str(settled_odds)) - Decimal("1.000")).quantize(Decimal("0.0001"))

    PredictionOutcome.objects.update_or_create(
        prediction_id=prediction.id,
        defaults={
            "result": result,
            "home_goals": home_goals,
            "away_goals": away_goals,
            "stake_units": Decimal("1.000"),
            "profit_units": profit,
            "settled_at": timezone.now(),
            "settlement_reason": "sprint7.10_manual_verified_final",
        },
    )


def apply_aug11_results(apps, schema_editor):
    _record(
        apps,
        home_contains="Helsingborg",
        away_contains="Varnamo",
        market="OVER_2_5",
        home_goals=1,
        away_goals=2,
        result="WIN",
        odds="1.62",
        rank=2,
        tier="B",
    )
    _record(
        apps,
        home_contains="Fluminense",
        away_contains="Rivadavia",
        market="BTTS",
        home_goals=0,
        away_goals=0,
        result="LOSS",
        rank=1,
        tier="A",
    )
    _record(
        apps,
        home_contains="Avai",
        away_contains="CRB",
        market="OVER_2_5",
        home_goals=0,
        away_goals=1,
        result="LOSS",
        odds="2.07",
        rank=3,
        tier="B",
    )


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("engine", "0007_record_toluca_w_loss"),
        ("backtesting", "0001_initial"),
    ]

    operations = [migrations.RunPython(apply_aug11_results, reverse_noop)]
