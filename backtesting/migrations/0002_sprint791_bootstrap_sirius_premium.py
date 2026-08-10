from decimal import Decimal

from django.db import migrations
from django.utils import timezone


def bootstrap_first_premium(apps, schema_editor):
    Prediction = apps.get_model("engine", "Prediction")
    Ledger = apps.get_model("engine", "PremiumPublicationLedger")
    Outcome = apps.get_model("backtesting", "PredictionOutcome")

    prediction = (
        Prediction.objects.filter(
            fixture__kickoff__date="2026-08-10",
            fixture__home_team__name__icontains="Sirius",
            fixture__away_team__name__icontains="Brommapojkarna",
            market="BTTS",
            market_odds=Decimal("1.620"),
        )
        .order_by("-score", "id")
        .first()
    )
    if prediction is None:
        return

    fixture = prediction.fixture
    Ledger.objects.get_or_create(
        prediction_id=prediction.id,
        defaults={
            "target_date": "2026-08-10",
            "published_rank": 1,
            "premium_tier": "B",
            "premium_rank_score": Decimal("74.80"),
            "model_version": prediction.model_version,
            "market": "BTTS",
            "selection": prediction.selection,
            "odds": Decimal("1.620"),
            "snapshot": {
                "home_team": fixture.home_team.name,
                "away_team": fixture.away_team.name,
                "market": "BTTS",
                "selection": prediction.selection,
                "odds": 1.62,
                "official_source": "confirmed Premium #1 before immutable ledger rollout",
            },
        },
    )

    # User-confirmed official result: BTTS YES won. If provider final goals are
    # already present preserve them; otherwise history can still show WIN/P&L.
    Outcome.objects.update_or_create(
        prediction_id=prediction.id,
        defaults={
            "result": "WIN",
            "home_goals": fixture.home_goals,
            "away_goals": fixture.away_goals,
            "stake_units": Decimal("1.000"),
            "profit_units": Decimal("0.6200"),
            "settled_at": timezone.now(),
            "settlement_reason": "legacy_official_premium_confirmed_win",
        },
    )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("engine", "0005_premium_publication_ledger"),
        ("backtesting", "0001_initial"),
    ]

    operations = [migrations.RunPython(bootstrap_first_premium, noop)]
