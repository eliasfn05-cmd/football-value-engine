from django.db import migrations


TARGET_DATE = "2026-08-14"


def _norm(value):
    return " ".join(str(value or "").strip().lower().split())


def remove_reims_dunkerque_premium_lock(apps, schema_editor):
    DailyPremiumSelection = apps.get_model("engine", "DailyPremiumSelection")
    PremiumPublicationLedger = apps.get_model("engine", "PremiumPublicationLedger")

    daily_delete_ids = []
    for row in DailyPremiumSelection.objects.select_related(
        "prediction",
        "prediction__fixture",
        "prediction__fixture__home_team",
        "prediction__fixture__away_team",
    ).filter(target_date=TARGET_DATE, prediction__market="OVER_2_5"):
        fixture = row.prediction.fixture
        home = _norm(fixture.home_team.name)
        away = _norm(fixture.away_team.name)
        if ("reims" in home and "dunkerque" in away) or ("reims" in away and "dunkerque" in home):
            daily_delete_ids.append(row.id)

    if daily_delete_ids:
        DailyPremiumSelection.objects.filter(id__in=daily_delete_ids).delete()

    publication_delete_ids = []
    for row in PremiumPublicationLedger.objects.select_related(
        "prediction",
        "prediction__fixture",
        "prediction__fixture__home_team",
        "prediction__fixture__away_team",
    ).filter(target_date=TARGET_DATE, market="OVER_2_5"):
        fixture = row.prediction.fixture
        home = _norm(fixture.home_team.name)
        away = _norm(fixture.away_team.name)
        if ("reims" in home and "dunkerque" in away) or ("reims" in away and "dunkerque" in home):
            publication_delete_ids.append(row.id)

    if publication_delete_ids:
        PremiumPublicationLedger.objects.filter(id__in=publication_delete_ids).delete()


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("engine", "0010_enforce_premium_odds_and_backfill_aug13"),
    ]

    operations = [
        migrations.RunPython(remove_reims_dunkerque_premium_lock, reverse_noop),
    ]
