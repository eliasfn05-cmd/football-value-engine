from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("backtesting", "0011_correct_stockholm_jarfalla_outcome_20260829")]

    operations = [
        migrations.CreateModel(
            name="BttsV293HoldoutSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("target_date", models.DateField(db_index=True)),
                ("rank", models.PositiveSmallIntegerField(default=1)),
                ("challenger_version", models.CharField(default="V2.9.3-FROZEN", max_length=40)),
                ("recalibrated_score", models.DecimalField(decimal_places=5, max_digits=9)),
                ("raw_score", models.DecimalField(decimal_places=5, max_digits=9)),
                ("market_odds", models.DecimalField(blank=True, decimal_places=4, max_digits=9, null=True)),
                ("empirical_btts", models.DecimalField(decimal_places=6, max_digits=8)),
                ("consensus_probability", models.DecimalField(decimal_places=6, max_digits=8)),
                ("calibrated_probability", models.DecimalField(decimal_places=6, max_digits=8)),
                ("weakest_probability", models.DecimalField(decimal_places=6, max_digits=8)),
                ("snapshot", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("fixture", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="v293_holdout_snapshots", to="engine.fixture")),
                ("prediction", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="v293_holdout_snapshots", to="engine.prediction")),
            ],
            options={
                "ordering": ["target_date", "rank", "id"],
                "constraints": [models.UniqueConstraint(fields=("target_date", "challenger_version", "rank"), name="uniq_v293_holdout_day_version_rank")],
                "indexes": [models.Index(fields=["challenger_version", "target_date"], name="v293_holdout_ver_date_idx")],
            },
        ),
    ]
