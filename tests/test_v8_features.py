from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from engine.features import FeatureEngineeringService
from engine.models import Fixture, Team


class FeatureEngineeringTests(TestCase):
    def setUp(self):
        self.home = Team.objects.create(external_id="home", name="Home")
        self.away = Team.objects.create(external_id="away", name="Away")
        self.other = Team.objects.create(external_id="other", name="Other")
        self.kickoff = timezone.now() + timedelta(days=1)

    def _fixture(self, idx, home, away, hg, ag, days_before):
        return Fixture.objects.create(
            external_id=f"hist-{idx}",
            competition="Test League",
            kickoff=self.kickoff - timedelta(days=days_before),
            home_team=home,
            away_team=away,
            status="FT",
            home_goals=hg,
            away_goals=ag,
        )

    def test_away_profile_uses_only_away_matches(self):
        # Away team: five low-scoring away matches => 0% Over 2.5 away.
        for idx in range(5):
            self._fixture(idx, self.other, self.away, 1, 1 if idx < 2 else 0, 10 + idx)

        # High-scoring home games must not contaminate its away profile.
        for idx in range(5, 8):
            self._fixture(idx, self.away, self.other, 4, 2, 20 + idx)

        target = Fixture.objects.create(
            external_id="target",
            competition="Test League",
            kickoff=self.kickoff,
            home_team=self.home,
            away_team=self.away,
            status="NS",
        )

        vector = FeatureEngineeringService().build(target)
        self.assertEqual(vector.away_profile.sample_size, 5)
        self.assertEqual(vector.away_over25_last5_away, 0.0)
        self.assertEqual(vector.away_btts_last5_away, 0.4)

    def test_data_quality_is_low_without_contextual_history_or_market(self):
        target = Fixture.objects.create(
            external_id="quality-target",
            competition="Test League",
            kickoff=self.kickoff,
            home_team=self.home,
            away_team=self.away,
            status="NS",
        )
        vector = FeatureEngineeringService().build(target)
        self.assertLess(vector.data_quality_score, 50)
