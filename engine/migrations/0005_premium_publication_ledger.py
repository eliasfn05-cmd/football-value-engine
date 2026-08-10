from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("engine", "0004_daily_premium_selection")]

    operations = [
        migrations.CreateModel(
            name="PremiumPublicationLedger",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("target_date", models.DateField(db_index=True)),
                ("published_rank", models.PositiveSmallIntegerField()),
                ("premium_tier", models.CharField(max_length=1)),
                ("premium_rank_score", models.DecimalField(decimal_places=2, max_digits=5)),
                ("model_version", models.CharField(db_index=True, max_length=30)),
                ("market", models.CharField(max_length=60)),
                ("selection", models.CharField(max_length=80)),
                ("odds", models.DecimalField(decimal_places=3, max_digits=7)),
                ("snapshot", models.JSONField(blank=True, default=dict)),
                ("published_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("prediction", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="premium_publication", to="engine.prediction")),
            ],
            options={"ordering": ["-published_at"]},
        ),
        migrations.AddIndex(
            model_name="premiumpublicationledger",
            index=models.Index(fields=["target_date", "model_version"], name="premledger_date_model_idx"),
        ),
    ]
