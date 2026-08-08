from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("scanner", "0001_pipeline_observability"),
    ]

    operations = [
        migrations.CreateModel(
            name="PremiumGenerationJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("target_date", models.DateField(db_index=True)),
                ("mode", models.CharField(default="full", max_length=16)),
                ("status", models.CharField(choices=[("QUEUED", "Queued"), ("DISPATCHED", "Dispatched"), ("RUNNING", "Running"), ("SUCCESS", "Success"), ("PARTIAL", "Partial"), ("FAILED", "Failed")], db_index=True, default="QUEUED", max_length=12)),
                ("current_stage", models.CharField(blank=True, max_length=32)),
                ("progress_pct", models.PositiveSmallIntegerField(default=0)),
                ("message", models.CharField(blank=True, max_length=255)),
                ("requested_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("dispatched_at", models.DateTimeField(blank=True, null=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("pipeline", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="generation_job", to="scanner.pipelinerun")),
            ],
            options={
                "ordering": ["-requested_at"],
                "indexes": [models.Index(fields=["target_date", "status"], name="premium_job_date_status_idx")],
            },
        ),
    ]
