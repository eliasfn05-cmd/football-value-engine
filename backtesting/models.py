from django.db import models

from engine.models import Prediction


class PredictionOutcome(models.Model):
    RESULT_PENDING = "PENDING"
    RESULT_WIN = "WIN"
    RESULT_LOSS = "LOSS"
    RESULT_VOID = "VOID"
    RESULT_CHOICES = [
        (RESULT_PENDING, "Pending"),
        (RESULT_WIN, "Win"),
        (RESULT_LOSS, "Loss"),
        (RESULT_VOID, "Void"),
    ]

    prediction = models.OneToOneField(
        Prediction,
        on_delete=models.CASCADE,
        related_name="outcome",
    )
    result = models.CharField(max_length=12, choices=RESULT_CHOICES, default=RESULT_PENDING, db_index=True)
    home_goals = models.PositiveSmallIntegerField(null=True, blank=True)
    away_goals = models.PositiveSmallIntegerField(null=True, blank=True)
    stake_units = models.DecimalField(max_digits=7, decimal_places=3, default=1)
    profit_units = models.DecimalField(max_digits=9, decimal_places=4, default=0)
    settled_at = models.DateTimeField(null=True, blank=True, db_index=True)
    settlement_reason = models.CharField(max_length=160, blank=True)

    def __str__(self):
        return f"{self.prediction_id} {self.result}"


class LearningSnapshot(models.Model):
    model_version = models.CharField(max_length=40, db_index=True)
    scope = models.CharField(max_length=80, db_index=True)
    sample_size = models.PositiveIntegerField(default=0)
    wins = models.PositiveIntegerField(default=0)
    losses = models.PositiveIntegerField(default=0)
    voids = models.PositiveIntegerField(default=0)
    win_rate = models.DecimalField(max_digits=7, decimal_places=5, null=True, blank=True)
    roi = models.DecimalField(max_digits=9, decimal_places=5, null=True, blank=True)
    yield_pct = models.DecimalField(max_digits=9, decimal_places=5, null=True, blank=True)
    avg_probability = models.DecimalField(max_digits=7, decimal_places=5, null=True, blank=True)
    avg_edge = models.DecimalField(max_digits=7, decimal_places=5, null=True, blank=True)
    avg_expected_value = models.DecimalField(max_digits=7, decimal_places=5, null=True, blank=True)
    total_profit_units = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["model_version", "scope", "created_at"], name="learn_model_scope_idx"),
        ]

    def __str__(self):
        return f"{self.model_version} {self.scope} n={self.sample_size}"
