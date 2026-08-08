from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("engine", "0002_sprint4_data_models"),
    ]

    operations = [
        migrations.CreateModel(
            name="FixtureScoreState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("model_version", models.CharField(max_length=30)),
                ("feature_fingerprint", models.CharField(max_length=64)),
                ("scored_at", models.DateTimeField(auto_now=True, db_index=True)),
                (
                    "fixture",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="score_states",
                        to="engine.fixture",
                    ),
                ),
            ],
            options={
                "indexes": [models.Index(fields=["model_version", "scored_at"], name="scorestate_model_time_idx")],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("fixture", "model_version"),
                        name="uniq_fixture_model_score_state",
                    )
                ],
            },
        ),
    ]
