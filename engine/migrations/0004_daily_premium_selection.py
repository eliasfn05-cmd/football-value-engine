from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("engine", "0003_fixture_score_state"),
    ]

    operations = [
        migrations.CreateModel(
            name="DailyPremiumSelection",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("target_date", models.DateField(db_index=True)),
                ("rank", models.PositiveSmallIntegerField()),
                ("premium_tier", models.CharField(choices=[("A", "Premium A"), ("B", "Premium B"), ("C", "Premium C")], max_length=1)),
                ("premium_rank_score", models.DecimalField(decimal_places=2, max_digits=5)),
                ("model_version", models.CharField(max_length=30)),
                ("rationale", models.JSONField(blank=True, default=dict)),
                ("selected_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("prediction", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="daily_selections", to="engine.prediction")),
            ],
            options={
                "ordering": ["target_date", "rank"],
                "indexes": [models.Index(fields=["target_date", "model_version", "rank"], name="premium_date_model_rank_idx")],
                "constraints": [
                    models.UniqueConstraint(fields=("target_date", "rank"), name="uniq_daily_premium_rank"),
                    models.UniqueConstraint(fields=("target_date", "prediction"), name="uniq_daily_premium_prediction"),
                ],
            },
        ),
    ]
