from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="PipelineRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("target_date", models.DateField(db_index=True)),
                ("status", models.CharField(choices=[("RUNNING", "Running"), ("SUCCESS", "Success"), ("PARTIAL", "Partial"), ("FAILED", "Failed")], db_index=True, default="RUNNING", max_length=12)),
                ("started_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("duration_seconds", models.PositiveIntegerField(default=0)),
                ("fixtures_count", models.PositiveIntegerField(default=0)),
                ("predictions_count", models.PositiveIntegerField(default=0)),
                ("premium_count", models.PositiveIntegerField(default=0)),
                ("settled_count", models.PositiveIntegerField(default=0)),
                ("warning_count", models.PositiveIntegerField(default=0)),
                ("error_count", models.PositiveIntegerField(default=0)),
                ("metadata", models.JSONField(blank=True, default=dict)),
            ],
            options={"ordering": ["-started_at"]},
        ),
        migrations.CreateModel(
            name="PipelineStageRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(db_index=True, max_length=32)),
                ("status", models.CharField(choices=[("RUNNING", "Running"), ("SUCCESS", "Success"), ("WARNING", "Warning"), ("FAILED", "Failed")], db_index=True, default="RUNNING", max_length=12)),
                ("attempt_count", models.PositiveSmallIntegerField(default=1)),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("duration_seconds", models.PositiveIntegerField(default=0)),
                ("records_processed", models.PositiveIntegerField(default=0)),
                ("message", models.CharField(blank=True, max_length=255)),
                ("details", models.JSONField(blank=True, default=dict)),
                ("pipeline", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="stages", to="scanner.pipelinerun")),
            ],
            options={"ordering": ["started_at"]},
        ),
        migrations.AddIndex(
            model_name="pipelinerun",
            index=models.Index(fields=["target_date", "status"], name="pipeline_date_status_idx"),
        ),
        migrations.AddIndex(
            model_name="pipelinestagerun",
            index=models.Index(fields=["pipeline", "name"], name="pipeline_stage_idx"),
        ),
    ]
