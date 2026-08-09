from __future__ import annotations

import time
from collections import defaultdict
from statistics import mean
from typing import Callable

from django.db.models import F, Q, Window
from django.db.models.functions import RowNumber

from .features import FeatureEngineeringService, FeatureVector, VenueProfile
from .models import Fixture, LineupSnapshot, OddsSnapshot, StandingSnapshot


class BatchFeatureEngineeringService:
    """Bounded SQL preload for scoring a complete date against remote PostgreSQL."""

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

    def _phase(self, name: str, fn) -> None:
        started = time.perf_counter()
        self.progress(f"[features] START {name}")
        fn()
        elapsed = time.perf_counter() - started
        self.progress(f"[features] DONE {name} {elapsed:.2f}s")

    def preload(self) -> None:
        if not self.fixtures:
            return
        self.progress(
            f"[features] preload teams={len(self.team_ids)} fixtures={len(self.fixture_ids)} sample={self.venue_sample_size}"
        )
        self._phase("history", self._preload_history)
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
            goals_for=round(mean(gf_values), 3),
            goals_against=round(mean(ga_values), 3),
            over25_rate=round(overs / n, 3),
            btts_rate=round(btts / n, 3),
            clean_sheet_rate=round(clean / n, 3),
            failed_to_score_rate=round(fts / n, 3),
            btts_over25_escalation_rate=round(escalation, 3),
            low_score_rate=round(low_score / n, 3),
            one_one_rate=round(one_one / n, 3),
        )

    @staticmethod
    def _player_ids(snapshot: LineupSnapshot | None) -> set[str]:
        return FeatureEngineeringService._lineup_player_ids(snapshot)

    def _continuity(self, fixture_id: int, team_id: int) -> float | None:
        current = self._lineups_current.get((fixture_id, team_id))
        previous = self._previous_lineup_by_team.get(team_id)
        current_ids = self._player_ids(current)
        previous_ids = self._player_ids(previous)
        if not current_ids or not previous_ids:
            return None
        denominator = max(1, min(11, len(current_ids), len(previous_ids)))
        return round(len(current_ids & previous_ids) / denominator, 3)

    def build(self, fixture: Fixture) -> FeatureVector:
        home = self._profile(self._history.get((fixture.home_team_id, "home"), []), "home")
        away = self._profile(self._history.get((fixture.away_team_id, "away"), []), "away")

        home_position = away_position = None
        home_ppg = away_ppg = None
        if fixture.competition_ref_id:
            home_row = self._standings.get((fixture.competition_ref_id, fixture.home_team_id))
            away_row = self._standings.get((fixture.competition_ref_id, fixture.away_team_id))
            if home_row:
                home_position = home_row.position
                home_ppg = round(home_row.points / home_row.played, 3) if home_row.played else None
            if away_row:
                away_position = away_row.position
                away_ppg = round(away_row.points / away_row.played, 3) if away_row.played else None

        home_lineup = self._continuity(fixture.id, fixture.home_team_id)
        away_lineup = self._continuity(fixture.id, fixture.away_team_id)
        btts_odds = self._odds.get((fixture.id, "BTTS", "YES"))
        over_odds = self._odds.get((fixture.id, "OVER_2_5", "OVER"))
        quality = FeatureEngineeringService._quality_score(
            home, away, home_position, away_position, home_lineup, away_lineup, btts_odds, over_odds
        )

        return FeatureVector(
            fixture_id=fixture.external_id,
            home_team=fixture.home_team.name,
            away_team=fixture.away_team.name,
            home_profile=home,
            away_profile=away,
            home_over25_last5_home=home.over25_rate,
            away_over25_last5_away=away.over25_rate,
            home_btts_last5_home=home.btts_rate,
            away_btts_last5_away=away.btts_rate,
            home_clean_sheet_rate=home.clean_sheet_rate,
            away_clean_sheet_rate=away.clean_sheet_rate,
            home_failed_to_score_rate=home.failed_to_score_rate,
            away_failed_to_score_rate=away.failed_to_score_rate,
            home_table_position=home_position,
            away_table_position=away_position,
            home_points_per_game=home_ppg,
            away_points_per_game=away_ppg,
            home_lineup_continuity=home_lineup,
            away_lineup_continuity=away_lineup,
            btts_market_odds=btts_odds,
            over25_market_odds=over_odds,
            data_quality_score=quality,
        )
