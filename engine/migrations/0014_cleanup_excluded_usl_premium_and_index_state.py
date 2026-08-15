from django.db import migrations
from django.db.models import Q


def purge_excluded_usl_premium(apps, schema_editor):
    DailyPremiumSelection = apps.get_model("engine", "DailyPremiumSelection")
    PremiumPublicationLedger = apps.get_model("engine", "PremiumPublicationLedger")

    usl_filter = (
        Q(prediction__fixture__competition__icontains="USL League One")
        | Q(prediction__fixture__competition__icontains="USL Cup")
        | Q(prediction__fixture__competition_ref__name__icontains="USL League One")
        | Q(prediction__fixture__competition_ref__name__icontains="USL Cup")
    )

    # The operational shortlist must be cleared first. Otherwise a stale row can
    # survive on the dashboard even after the competition has been hard-excluded.
    DailyPremiumSelection.objects.filter(usl_filter).delete()

    # Publication locks are intentionally immutable for history, but an invalid
    # league classification must not be allowed to resurrect a now-excluded pick
    # during reconcile(). Removing these invalid locks lets the selector choose a
    # replacement or correctly return fewer than three Premium picks.
    PremiumPublicationLedger.objects.filter(usl_filter).delete()


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("engine", "0013_add_scoring_preload_indexes"),
    ]

    operations = [
        # Keep migration state synchronized with the current models.py. These
        # indexes were introduced for a short-lived preload strategy and removed
        # from model Meta, but the state migration had been missing.
        migrations.RemoveIndex(model_name="fixture", name="fixture_home_kick_idx"),
        migrations.RemoveIndex(model_name="fixture", name="fixture_away_kick_idx"),
        migrations.RemoveIndex(model_name="lineupsnapshot", name="lineup_team_cap_idx"),
        migrations.RemoveIndex(model_name="oddssnapshot", name="odds_fix_mkt_sel_cap_idx"),
        migrations.RemoveIndex(model_name="standingsnapshot", name="stand_comp_team_cap_idx"),
        migrations.RunPython(purge_excluded_usl_premium, noop_reverse),
    ]
