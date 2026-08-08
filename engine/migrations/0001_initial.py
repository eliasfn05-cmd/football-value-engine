from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True
    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Team",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("external_id", models.CharField(max_length=64, unique=True)),
                ("name", models.CharField(max_length=160)),
                ("country", models.CharField(blank=True, max_length=100)),
            ],
        ),
        migrations.CreateModel(
            name="Fixture",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("external_id", models.CharField(max_length=64, unique=True)),
                ("competition", models.CharField(max_length=160)),
                ("kickoff", models.DateTimeField(db_index=True)),
                ("venue", models.CharField(blank=True, max_length=200)),
                ("status", models.CharField(default="scheduled", max_length=40)),
                ("home_goals", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("away_goals", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("away_team", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="away_fixtures", to="engine.team")),
                ("home_team", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="home_fixtures", to="engine.team")),
            ],
        ),
        migrations.CreateModel(
            name="OddsSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("bookmaker", models.CharField(max_length=100)),
                ("market", models.CharField(max_length=60)),
                ("selection", models.CharField(max_length=80)),
                ("decimal_odds", models.DecimalField(decimal_places=3, max_digits=7)),
                ("captured_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("fixture", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="odds", to="engine.fixture")),
            ],
        ),
        migrations.CreateModel(
            name="Prediction",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("model_version", models.CharField(max_length=30)),
                ("market", models.CharField(max_length=60)),
                ("selection", models.CharField(max_length=80)),
                ("probability", models.DecimalField(decimal_places=5, max_digits=6)),
                ("fair_odds", models.DecimalField(blank=True, decimal_places=3, max_digits=7, null=True)),
                ("market_odds", models.DecimalField(blank=True, decimal_places=3, max_digits=7, null=True)),
                ("edge", models.DecimalField(blank=True, decimal_places=5, max_digits=7, null=True)),
                ("expected_value", models.DecimalField(blank=True, decimal_places=5, max_digits=7, null=True)),
                ("score", models.DecimalField(decimal_places=2, default=0, max_digits=5)),
                ("tier", models.CharField(blank=True, max_length=20)),
                ("reasons", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("fixture", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="predictions", to="engine.fixture")),
            ],
        ),
        migrations.AddIndex(
            model_name="oddssnapshot",
            index=models.Index(fields=["fixture", "market", "captured_at"], name="engine_odds_fixture_market_idx"),
        ),
        migrations.AddIndex(
            model_name="prediction",
            index=models.Index(fields=["model_version", "tier", "created_at"], name="engine_pred_model_tier_idx"),
        ),
    ]
