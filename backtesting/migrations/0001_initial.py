from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("engine", "0002_sprint4_data_models"),
    ]

    operations = [
        migrations.CreateModel(
            name="LearningSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("model_version", models.CharField(db_index=True, max_length=40)),
                ("scope", models.CharField(db_index=True, max_length=80)),
                ("sample_size", models.PositiveIntegerField(default=0)),
                ("wins", models.PositiveIntegerField(default=0)),
                ("losses", models.PositiveIntegerField(default=0)),
                ("voids", models.PositiveIntegerField(default=0)),
                ("win_rate", models.DecimalField(blank=True, decimal_places=5, max_digits=7, null=True)),
                ("roi", models.DecimalField(blank=True, decimal_places=5, max_digits=9, null=True)),
                ("yield_pct", models.DecimalField(blank=True, decimal_places=5, max_digits=9, null=True)),
                ("avg_probability", models.DecimalField(blank=True, decimal_places=5, max_digits=7, null=True)),
                ("avg_edge", models.DecimalField(blank=True, decimal_places=5, max_digits=7, null=True)),
                ("avg_expected_value", models.DecimalField(blank=True, decimal_places=5, max_digits=7, null=True)),
                ("total_profit_units", models.DecimalField(decimal_places=4, default=0, max_digits=10)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
            ],
        ),
        migrations.CreateModel(
            name="PredictionOutcome",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("result", models.CharField(choices=[("PENDING", "Pending"), ("WIN", "Win"), ("LOSS", "Loss"), ("VOID", "Void")], db_index=True, default="PENDING", max_length=12)),
                ("home_goals", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("away_goals", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("stake_units", models.DecimalField(decimal_places=3, default=1, max_digits=7)),
                ("profit_units", models.DecimalField(decimal_places=4, default=0, max_digits=9)),
                ("settled_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("settlement_reason", models.CharField(blank=True, max_length=160)),
                ("prediction", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="outcome", to="engine.prediction")),
            ],
        ),
        migrations.AddIndex(
            model_name="learningsnapshot",
            index=models.Index(fields=["model_version", "scope", "created_at"], name="learn_model_scope_idx"),
        ),
    ]
