from django.db import models


class Competition(models.Model):
    external_id = models.CharField(max_length=64)
    name = models.CharField(max_length=160)
    country = models.CharField(max_length=100, blank=True)
    season = models.PositiveSmallIntegerField(null=True, blank=True)
    competition_type = models.CharField(max_length=40, blank=True)
    logo = models.URLField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["external_id", "season"],
                name="uniq_competition_external_season",
            )
        ]

    def __str__(self):
        return f"{self.name} {self.season or ''}".strip()


class Team(models.Model):
    external_id = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=160)
    country = models.CharField(max_length=100, blank=True)
    logo = models.URLField(blank=True)
    venue_name = models.CharField(max_length=200, blank=True)
    venue_capacity = models.PositiveIntegerField(null=True, blank=True)

    def __str__(self):
        return self.name


class Fixture(models.Model):
    external_id = models.CharField(max_length=64, unique=True)
    competition = models.CharField(max_length=160)
    competition_ref = models.ForeignKey(
        Competition,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="fixtures",
    )
    season = models.PositiveSmallIntegerField(null=True, blank=True)
    round = models.CharField(max_length=120, blank=True)
    kickoff = models.DateTimeField(db_index=True)
    home_team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="home_fixtures")
    away_team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="away_fixtures")
    venue = models.CharField(max_length=200, blank=True)
    venue_city = models.CharField(max_length=120, blank=True)
    referee = models.CharField(max_length=160, blank=True)
    status = models.CharField(max_length=40, default="scheduled")
    home_goals = models.PositiveSmallIntegerField(null=True, blank=True)
    away_goals = models.PositiveSmallIntegerField(null=True, blank=True)

    def __str__(self):
        return f"{self.home_team} vs {self.away_team}"


class StandingSnapshot(models.Model):
    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name="standings")
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="standings")
    position = models.PositiveSmallIntegerField()
    played = models.PositiveSmallIntegerField(default=0)
    won = models.PositiveSmallIntegerField(default=0)
    draw = models.PositiveSmallIntegerField(default=0)
    lost = models.PositiveSmallIntegerField(default=0)
    goals_for = models.PositiveSmallIntegerField(default=0)
    goals_against = models.PositiveSmallIntegerField(default=0)
    points = models.SmallIntegerField(default=0)
    form = models.CharField(max_length=30, blank=True)
    captured_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["competition", "captured_at"])]


class TeamStatisticsSnapshot(models.Model):
    fixture = models.ForeignKey(Fixture, on_delete=models.CASCADE, related_name="team_statistics")
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="fixture_statistics")
    is_home = models.BooleanField(default=False)
    statistics = models.JSONField(default=dict)
    captured_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["fixture", "team", "captured_at"])]


class LineupSnapshot(models.Model):
    fixture = models.ForeignKey(Fixture, on_delete=models.CASCADE, related_name="lineups")
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="lineups")
    formation = models.CharField(max_length=40, blank=True)
    coach_name = models.CharField(max_length=160, blank=True)
    starting_xi = models.JSONField(default=list)
    substitutes = models.JSONField(default=list)
    captured_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["fixture", "team", "captured_at"])]


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
