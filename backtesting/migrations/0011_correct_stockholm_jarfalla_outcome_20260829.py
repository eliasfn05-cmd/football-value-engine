from django.db import migrations


def correct_stockholm_jarfalla(apps, schema_editor):
    Fixture = apps.get_model("engine", "Fixture")
    Prediction = apps.get_model("engine", "Prediction")
    Ledger = apps.get_model("engine", "PremiumPublicationLedger")
    Outcome = apps.get_model("backtesting", "PredictionOutcome")

    fixture = Fixture.objects.filter(id=30893).first()
    if fixture is None:
        return

    # User-verified 90-minute result.
    if fixture.home_goals != 2 or fixture.away_goals != 0:
        fixture.home_goals = 2
        fixture.away_goals = 0
        fixture.save(update_fields=["home_goals", "away_goals"])

    prediction = Prediction.objects.filter(id=7747, fixture_id=30893, market__iexact="BTTS").first()
    if prediction is None:
        return

    # Only correct the official Premium prediction already backed by the immutable ledger.
    ledger = Ledger.objects.filter(prediction_id=prediction.id).first()
    if ledger is None:
        return

    Outcome.objects.update_or_create(
        prediction_id=prediction.id,
        defaults={
            "result": "LOSS",
            "home_goals": 2,
            "away_goals": 0,
            "stake_units": 1,
            "profit_units": -1,
            "settlement_reason": "manual_verified_stockholm_jarfalla_20260829",
        },
    )


def reverse(apps, schema_editor):
    # This migration fixes a verified real-world result. Do not restore the known-bad 0-0.
    pass


class Migration(migrations.Migration):
    dependencies = [("backtesting", "0010_correct_verified_btts_results_20260829")]
    operations = [migrations.RunPython(correct_stockholm_jarfalla, reverse)]
