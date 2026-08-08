from django.db import models


class PipelineRun(models.Model):
    STATUS_RUNNING = "RUNNING"
    STATUS_SUCCESS = "SUCCESS"
    STATUS_PARTIAL = "PARTIAL"
    STATUS_FAILED = "FAILED"
    STATUS_CHOICES = [
        (STATUS_RUNNING, "Running"),
        (STATUS_SUCCESS, "Success"),
        (STATUS_PARTIAL, "Partial"),
        (STATUS_FAILED, "Failed"),
    ]

    target_date = models.DateField(db_index=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_RUNNING, db_index=True)
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(default=0)
    fixtures_count = models.PositiveIntegerField(default=0)
    predictions_count = models.PositiveIntegerField(default=0)
    premium_count = models.PositiveIntegerField(default=0)
    settled_count = models.PositiveIntegerField(default=0)
    warning_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["target_date", "status"], name="pipeline_date_status_idx"),
        ]

    def __str__(self):
        return f"Pipeline {self.target_date} {self.status}"


class PipelineStageRun(models.Model):
    STATUS_RUNNING = "RUNNING"
    STATUS_SUCCESS = "SUCCESS"
    STATUS_WARNING = "WARNING"
    STATUS_FAILED = "FAILED"
    STATUS_CHOICES = [
        (STATUS_RUNNING, "Running"),
        (STATUS_SUCCESS, "Success"),
        (STATUS_WARNING, "Warning"),
        (STATUS_FAILED, "Failed"),
    ]

    pipeline = models.ForeignKey(PipelineRun, on_delete=models.CASCADE, related_name="stages")
    name = models.CharField(max_length=32, db_index=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_RUNNING, db_index=True)
    attempt_count = models.PositiveSmallIntegerField(default=1)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(default=0)
    records_processed = models.PositiveIntegerField(default=0)
    message = models.CharField(max_length=255, blank=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["started_at"]
        indexes = [
            models.Index(fields=["pipeline", "name"], name="pipeline_stage_idx"),
        ]

    def __str__(self):
        return f"{self.pipeline_id} {self.name} {self.status}"
