from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("engine", "0012_record_aug13_aug14_premium_results"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="fixture",
            index=models.Index(fields=["home_team", "kickoff"], name="fixture_home_kick_idx"),
        ),
        migrations.AddIndex(
            model_name="fixture",
            index=models.Index(fields=["away_team", "kickoff"], name="fixture_away_kick_idx"),
        ),
        migrations.AddIndex(
            model_name="standingsnapshot",
            index=models.Index(fields=["competition", "team", "captured_at"], name="stand_comp_team_cap_idx"),
        ),
        migrations.AddIndex(
            model_name="lineupsnapshot",
            index=models.Index(fields=["team", "captured_at"], name="lineup_team_cap_idx"),
        ),
        migrations.AddIndex(
            model_name="oddssnapshot",
            index=models.Index(fields=["fixture", "market", "selection", "captured_at"], name="odds_fix_mkt_sel_cap_idx"),
        ),
    ]
