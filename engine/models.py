from django.db import models


class Team(models.Model):
    external_id = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=160)
    country = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return self.name


class Fixture(models.Model):
    external_id = models.CharField(max_length=64, unique=True)
    competition = models.CharField(max_length=160)
    kickoff = models.DateTimeField(db_index=True)
    home_team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="home_fixtures")
    away_team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="away_fixtures")
    venue = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=40, default="scheduled")
    home_goals = models.PositiveSmallIntegerField(null=True, blank=True)
    away_goals = models.PositiveSmallIntegerField(null=True, blank=True)

    def __str__(self):
        return f"{self.home_team} vs {self.away_team}"


class OddsSnapshot(models.Model):
    fixture = models.ForeignKey(Fixture, on_delete=models.CASCADE, related_name="odds")
    bookmaker = models.CharField(max_length=100)
    market = models.CharField(max_length=60)
    selection = models.CharField(max_length=80)
    decimal_odds = models.DecimalField(max_digits=7, decimal_places=3)
    captured_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["fixture", "market", "captured_at"])]


class Prediction(models.Model):
    fixture = models.ForeignKey(Fixture, on_delete=models.CASCADE, related_name="predictions")
    model_version = models.CharField(max_length=30)
    market = models.CharField(max_length=60)
    selection = models.CharField(max_length=80)
    probability = models.DecimalField(max_digits=6, decimal_places=5)
    fair_odds = models.DecimalField(max_digits=7, decimal_places=3, null=True, blank=True)
    market_odds = models.DecimalField(max_digits=7, decimal_places=3, null=True, blank=True)
    edge = models.DecimalField(max_digits=7, decimal_places=5, null=True, blank=True)
    expected_value = models.DecimalField(max_digits=7, decimal_places=5, null=True, blank=True)
    score = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    tier = models.CharField(max_length=20, blank=True)
    reasons = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["model_version", "tier", "created_at"])]
