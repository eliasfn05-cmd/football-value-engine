from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("engine", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="Competition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("external_id", models.CharField(max_length=64)),
                ("name", models.CharField(max_length=160)),
                ("country", models.CharField(blank=True, max_length=100)),
                ("season", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("competition_type", models.CharField(blank=True, max_length=40)),
                ("logo", models.URLField(blank=True)),
            ],
        ),
        migrations.AddField(model_name="team", name="logo", field=models.URLField(blank=True)),
        migrations.AddField(model_name="team", name="venue_name", field=models.CharField(blank=True, max_length=200)),
        migrations.AddField(model_name="team", name="venue_capacity", field=models.PositiveIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="fixture", name="season", field=models.PositiveSmallIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="fixture", name="round", field=models.CharField(blank=True, max_length=120)),
        migrations.AddField(model_name="fixture", name="venue_city", field=models.CharField(blank=True, max_length=120)),
        migrations.AddField(model_name="fixture", name="referee", field=models.CharField(blank=True, max_length=160)),
        migrations.AddField(
            model_name="fixture",
            name="competition_ref",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="fixtures", to="engine.competition"),
        ),
        migrations.CreateModel(
            name="StandingSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("position", models.PositiveSmallIntegerField()),
                ("played", models.PositiveSmallIntegerField(default=0)),
                ("won", models.PositiveSmallIntegerField(default=0)),
                ("draw", models.PositiveSmallIntegerField(default=0)),
                ("lost", models.PositiveSmallIntegerField(default=0)),
                ("goals_for", models.PositiveSmallIntegerField(default=0)),
                ("goals_against", models.PositiveSmallIntegerField(default=0)),
                ("points", models.SmallIntegerField(default=0)),
                ("form", models.CharField(blank=True, max_length=30)),
                ("captured_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("competition", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="standings", to="engine.competition")),
                ("team", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="standings", to="engine.team")),
            ],
        ),
        migrations.CreateModel(
            name="TeamStatisticsSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("is_home", models.BooleanField(default=False)),
                ("statistics", models.JSONField(default=dict)),
                ("captured_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("fixture", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="team_statistics", to="engine.fixture")),
                ("team", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="fixture_statistics", to="engine.team")),
            ],
        ),
        migrations.CreateModel(
            name="LineupSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("formation", models.CharField(blank=True, max_length=40)),
                ("coach_name", models.CharField(blank=True, max_length=160)),
                ("starting_xi", models.JSONField(default=list)),
                ("substitutes", models.JSONField(default=list)),
                ("captured_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("fixture", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lineups", to="engine.fixture")),
                ("team", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lineups", to="engine.team")),
            ],
        ),
        migrations.AddConstraint(
            model_name="competition",
            constraint=models.UniqueConstraint(fields=("external_id", "season"), name="uniq_competition_external_season"),
        ),
        migrations.AddIndex(
            model_name="standingsnapshot",
            index=models.Index(fields=["competition", "captured_at"], name="stand_comp_cap_idx"),
        ),
        migrations.AddIndex(
            model_name="teamstatisticssnapshot",
            index=models.Index(fields=["fixture", "team", "captured_at"], name="teamstat_fix_team_cap_idx"),
        ),
        migrations.AddIndex(
            model_name="lineupsnapshot",
            index=models.Index(fields=["fixture", "team", "captured_at"], name="lineup_fix_team_cap_idx"),
        ),
    ]
