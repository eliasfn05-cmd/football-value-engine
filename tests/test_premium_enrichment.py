from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from engine.models import Fixture, Team
from scanner.management.commands.enrich_candidates import Command, MIN_VENUE_SAMPLE


class PremiumEnrichmentTests(TestCase):
    def setUp(self):
        self.home = Team.objects.create(external_id="premium-home", name="Premium Home")
        self.away = Team.objects.create(external_id="premium-away", name="Premium Away")
        self.other = Team.objects.create(external_id="premium-other", name="Premium Other")
        self.kickoff = timezone.now() + timedelta(hours=6)
        self.target = Fixture.objects.create(
            external_id="premium-target",
            competition="Premium League",
            kickoff=self.kickoff,
            home_team=self.home,
            away_team=self.away,
            status="NS",
        )

    def _finished(self, *, external_id: str, kickoff, home: Team, away: Team):
        return Fixture.objects.create(
            external_id=external_id,
            competition="Historical League",
            kickoff=kickoff,
            home_team=home,
            away_team=away,
            status="FT",
            home_goals=1,
            away_goals=1,
        )

    def test_detects_missing_home_and_away_venue_samples(self):
        gaps = Command._teams_missing_venue_history([self.target])
        by_team = {team.id: venues for team, _before, venues in gaps}
        self.assertEqual(by_team[self.home.id], {"home"})
        self.assertEqual(by_team[self.away.id], {"away"})

    def test_stops_backfill_after_minimum_venue_sample_is_available(self):
        for index in range(MIN_VENUE_SAMPLE):
            opponent_home = Team.objects.create(
                external_id=f"hist-home-opponent-{index}",
                name=f"Hist Home Opponent {index}",
            )
            opponent_away = Team.objects.create(
                external_id=f"hist-away-opponent-{index}",
                name=f"Hist Away Opponent {index}",
            )
            self._finished(
                external_id=f"home-history-{index}",
                kickoff=self.kickoff - timedelta(days=10 + index),
                home=self.home,
                away=opponent_home,
            )
            self._finished(
                external_id=f"away-history-{index}",
                kickoff=self.kickoff - timedelta(days=20 + index),
                home=opponent_away,
                away=self.away,
            )

        gaps = Command._teams_missing_venue_history([self.target])
        self.assertEqual(gaps, [])

    def test_parallel_history_fetch_filters_rows_at_or_after_target_kickoff(self):
        past = self.kickoff - timedelta(days=2)
        future = self.kickoff + timedelta(hours=1)
        payload = [
            {"fixture": {"id": 101, "date": past.isoformat()}},
            {"fixture": {"id": 102, "date": self.kickoff.isoformat()}},
            {"fixture": {"id": 103, "date": future.isoformat()}},
        ]

        with patch("scanner.management.commands.enrich_candidates.APIFootballProvider") as provider_cls:
            provider_cls.return_value.team_recent_fixtures.return_value = payload
            result = Command._fetch_team_history(self.home.external_id, self.kickoff)

        self.assertEqual([row["fixture"]["id"] for row in result], [101])
        provider_cls.return_value.team_recent_fixtures.assert_called_once_with(
            self.home.external_id,
            last=20,
        )
