from __future__ import annotations

import os
import time
from collections import defaultdict
from statistics import mean
from typing import Callable

from django.db.models import F, Q, Window
from django.db.models.functions import RowNumber

from .features import FeatureEngineeringService, FeatureVector, VenueProfile
from .models import Fixture, LineupSnapshot, OddsSnapshot, StandingSnapshot


class BatchFeatureEngineeringService:
    """Bounded SQL preload for scoring a complete date against remote PostgreSQL.

    Interactive Premium generation has two distinct phases:
    1) a high-recall bootstrap over the daily card; and
    2) a full rescore of the small candidate pool.

    Interactive bootstrap must stay lightweight regardless of card size. Standings
    and lineup window queries are intentionally deferred to the shortlisted rescore,
    where the pool is small enough to run the complete feature set quickly.
    """

    INTERACTIVE_HEAVY_FEATURE_CUTOFF = 120

    def __init__(
        self,
        fixtures: list[Fixture],
        venue_sample_size: int = 5,
        *,
        progress: Callable[[str], None] | None = None,
    ):
        self.fixtures = fixtures
        self.venue_sample_size = venue_sample_size
        self.progress = progress or (lambda _message: None)
        self.team_ids = {f.home_team_id for f in fixtures} | {f.away_team_id for f in fixtures}
        self.fixture_ids = {f.id for f in fixtures}
        self.min_kickoff = min((f.kickoff for f in fixtures), default=None)
        self.max_kickoff = max((f.kickoff for f in fixtures), default=None)
        self._history: dict[tuple[int, str], list[Fixture]] = defaultdict(list)
        self._standings: dict[tuple[int, int], StandingSnapshot] = {}
        self._lineups_current: dict[tuple[int, int], LineupSnapshot] = {}
        self._previous_lineup_by_team: dict[int, LineupSnapshot] = {}
        self._odds: dict[tuple[int, str, str], float] = {}

    @staticmethod
    def _interactive_fast_enabled() -> bool:
        return os.getenv("PREMIUM_INTERACTIVE_FAST", "").strip().lower() in {"1", "true", "yes", "on"}

    def _use_lightweight_bootstrap(self) -> bool:
        # The interactive workflow immediately performs a full-feature rescore on
        # the shortlisted candidate pool. Running expensive standings/lineup
        # window queries here duplicates that work and was the main BOOTSTRAP_V8
        # bottleneck even on cards smaller than the old 120-fixture threshold.
        return self._interactive_fast_enabled()

    def _phase(self, name: str, fn) -> None:
        started = time.perf_counter()
        self.progress(f"[features] START {name}")
        fn()
        elapsed = time.perf_counter() - started
        self.progress(f"[features] DONE {name} {elapsed:.2f}s")

    def preload(self) -> None:
        if not self.fixtures:
            return
        lightweight = self._use_lightweight_bootstrap()
        self.progress(
            f"[features] preload teams={len(self.team_ids)} fixtures={len(self.fixture_ids)} "
            f"sample={self.venue_sample_size} lightweight_bootstrap={str(lightweight).lower()}"
        )
        self._phase("history", self._preload_history)
        if lightweight:
            self.progress(
                "[features] SKIP standings/lineups for interactive bootstrap; "
                "candidate rescore will restore full features"
            )
        else:
            self._phase("standings", self._preload_standings)
            self._phase("lineups", self._preload_lineups)
        self._phase("odds", self._preload_odds)

    def _preload_history(self) -> None:
        base_filters = {
            "kickoff__lt": self.min_kickoff,
            "home_goals__isnull": False,
            "away_goals__isnull": False,
        }
        home_qs = (
            Fixture.objects.filter(home_team_id__in=self.team_ids, **base_filters)
            .annotate(
                rn=Window(
                    expression=RowNumber(),
                    partition_by=[F("home_team_id")],
                    order_by=F("kickoff").desc(),
                )
            )
            .filter(rn__lte=self.venue_sample_size)
            .select_related("home_team", "away_team")
            .order_by("home_team_id", "-kickoff")
        )
        for item in home_qs.iterator(chunk_size=1000):
            self._history[(item.home_team_id, "home")].append(item)

        away_qs = (
            Fixture.objects.filter(away_team_id__in=self.team_ids, **base_filters)
            .annotate(
                rn=Window(
                    expression=RowNumber(),
                    partition_by=[F("away_team_id")],
                    order_by=F("kickoff").desc(),
                )
            )
            .filter(rn__lte=self.venue_sample_size)
            .select_related("home_team", "away_team")
            .order_by("away_team_id", "-kickoff")
        )
        for item in away_qs.iterator(chunk_size=1000):
            self._history[(item.away_team_id, "away")].append(item)

    def _preload_standings(self) -> None:
        competition_ids = {f.competition_ref_id for f in self.fixtures if f.competition_ref_id}
        if not competition_ids:
            return
        qs = (
            StandingSnapshot.objects.filter(
                competition_id__in=competition_ids,
                team_id__in=self.team_ids,
                captured_at__lte=self.min_kickoff,
            )
            .annotate(
                rn=Window(
                    expression=RowNumber(),
                    partition_by=[F("competition_id"), F("team_id")],
                    order_by=F("captured_at").desc(),
                )
            )
            .filter(rn=1)
            .order_by("competition_id", "team_id")
        )
        for row in qs.iterator(chunk_size=1000):
            self._standings[(row.competition_id, row.team_id)] = row

    def _preload_lineups(self) -> None:
        current_qs = (
            LineupSnapshot.objects.filter(fixture_id__in=self.fixture_ids, team_id__in=self.team_ids)
            .annotate(
                rn=Window(
                    expression=RowNumber(),
                    partition_by=[F("fixture_id"), F("team_id")],
                    order_by=F("captured_at").desc(),
                )
            )
            .filter(rn=1)
        )
        for row in current_qs.iterator(chunk_size=1000):
            self._lineups_current[(row.fixture_id, row.team_id)] = row

        previous_qs = (
            LineupSnapshot.objects.filter(
                team_id__in=self.team_ids,
                fixture__kickoff__lt=self.min_kickoff,
            )
            .annotate(
                rn=Window(
                    expression=RowNumber(),
                    partition_by=[F("team_id")],
                    order_by=[F("fixture__kickoff").desc(), F("captured_at").desc()],
                )
            )
            .filter(rn=1)
            .order_by("team_id")
        )
        for row in previous_qs.iterator(chunk_size=1000):
            self._previous_lineup_by_team[row.team_id] = row

    def _preload_odds(self) -> None:
        qs = (
            OddsSnapshot.objects.filter(fixture_id__in=self.fixture_ids)
            .filter(Q(market="BTTS", selection="YES") | Q(market="OVER_2_5", selection="OVER"))
            .annotate(
                rn=Window(
                    expression=RowNumber(),
                    partition_by=[F("fixture_id"), F("market"), F("selection")],
                    order_by=F("captured_at").desc(),
                )
            )
            .filter(rn=1)
            .order_by("fixture_id", "market", "selection")
        )
        for row in qs.iterator(chunk_size=1000):
            self._odds[(row.fixture_id, row.market, row.selection)] = float(row.decimal_odds)

    @staticmethod
    def _profile(fixtures: list[Fixture], venue: str) -> VenueProfile:
        if not fixtures:
            return VenueProfile(0, 1.20, 1.20, 0.50, 0.50, 0.20, 0.20)
        gf_values: list[int] = []
        ga_values: list[int] = []
        overs = btts = clean = fts = 0
        btts_and_over = low_score = one_one = 0
        for item in fixtures:
            hg = int(item.home_goals or 0)
            ag = int(item.away_goals or 0)
            gf, ga = (hg, ag) if venue == "home" else (ag, hg)
            total = gf + ga
            is_btts = gf > 0 and ga > 0
            gf_values.append(gf)
            ga_values.append(ga)
            overs += int(total >= 3)
            btts += int(is_btts)
            clean += int(ga == 0)
            fts += int(gf == 0)
            btts_and_over += int(is_btts and total >= 3)
            low_score += int(total <= 2)
            one_one += int(gf == 1 and ga == 1)
        n = len(fixtures)
        escalation = (btts_and_over / btts) if btts else 0.50
        return VenueProfile(
            sample_size=n,
            avg_goals_for=mean(gf_values),
            avg_goals_against=mean(ga_values),
            over25_rate=overs / n,
            btts_rate=btts / n,
            clean_sheet_rate=clean / n,
            failed_to_score_rate=fts / n,
            btts_over25_escalation=escalation,
            low_score_rate=low_score / n,
            one_one_rate=one_one / n,
        )

    def build(self, fixture: Fixture) -> FeatureVector:
        home_history = self._history.get((fixture.home_team_id, "home"), [])
        away_history = self._history.get((fixture.away_team_id, "away"), [])
        home = self._profile(home_history, "home")
        away = self._profile(away_history, "away")
        service = FeatureEngineeringService()
        standings = service._standing_features(
            fixture,
            home_row=self._standings.get((fixture.competition_ref_id, fixture.home_team_id)),
            away_row=self._standings.get((fixture.competition_ref_id, fixture.away_team_id)),
        )
        lineups = service._lineup_features(
            fixture,
            home_current=self._lineups_current.get((fixture.id, fixture.home_team_id)),
            away_current=self._lineups_current.get((fixture.id, fixture.away_team_id)),
            home_previous=self._previous_lineup_by_team.get(fixture.home_team_id),
            away_previous=self._previous_lineup_by_team.get(fixture.away_team_id),
        )
        odds = {
            "btts_yes": self._odds.get((fixture.id, "BTTS", "YES")),
            "over_2_5": self._odds.get((fixture.id, "OVER_2_5", "OVER")),
        }
        return FeatureVector(home=home, away=away, standings=standings, lineups=lineups, odds=odds)
