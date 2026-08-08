from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import mean
from typing import Any

from django.db.models import Q

from .models import Fixture, LineupSnapshot, OddsSnapshot, StandingSnapshot, Team


@dataclass(frozen=True)
class VenueProfile:
    sample_size: int
    goals_for: float
    goals_against: float
    over25_rate: float
    btts_rate: float
    clean_sheet_rate: float
    failed_to_score_rate: float


@dataclass(frozen=True)
class FeatureVector:
    fixture_id: str
    home_team: str
    away_team: str
    home_profile: VenueProfile
    away_profile: VenueProfile
    home_over25_last5_home: float
    away_over25_last5_away: float
    home_btts_last5_home: float
    away_btts_last5_away: float
    home_clean_sheet_rate: float
    away_clean_sheet_rate: float
    home_failed_to_score_rate: float
    away_failed_to_score_rate: float
    home_table_position: int | None
    away_table_position: int | None
    home_points_per_game: float | None
    away_points_per_game: float | None
    home_lineup_continuity: float | None
    away_lineup_continuity: float | None
    btts_market_odds: float | None
    over25_market_odds: float | None
    data_quality_score: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FeatureEngineeringService:
    """Build V8 features entirely from persisted data.

    This layer deliberately performs no external API calls. That makes feature
    generation reproducible for backtesting and allows model versions to be
    recalculated from the same stored evidence.
    """

    def __init__(self, venue_sample_size: int = 5):
        self.venue_sample_size = venue_sample_size

    @staticmethod
    def _finished_for_team(team: Team):
        return (
            Fixture.objects.filter(Q(home_team=team) | Q(away_team=team))
            .filter(home_goals__isnull=False, away_goals__isnull=False)
            .select_related("home_team", "away_team")
            .order_by("-kickoff")
        )

    def _venue_profile(self, team: Team, venue: str, before_fixture: Fixture) -> VenueProfile:
        qs = self._finished_for_team(team).filter(kickoff__lt=before_fixture.kickoff)
        qs = qs.filter(home_team=team) if venue == "home" else qs.filter(away_team=team)
        fixtures = list(qs[: self.venue_sample_size])

        if not fixtures:
            return VenueProfile(0, 1.20, 1.20, 0.50, 0.50, 0.20, 0.20)

        gf_values: list[int] = []
        ga_values: list[int] = []
        overs = btts = clean = fts = 0
        for item in fixtures:
            hg = int(item.home_goals or 0)
            ag = int(item.away_goals or 0)
            if venue == "home":
                gf, ga = hg, ag
            else:
                gf, ga = ag, hg
            gf_values.append(gf)
            ga_values.append(ga)
            overs += int(gf + ga >= 3)
            btts += int(gf > 0 and ga > 0)
            clean += int(ga == 0)
            fts += int(gf == 0)

        n = len(fixtures)
        return VenueProfile(
            sample_size=n,
            goals_for=round(mean(gf_values), 3),
            goals_against=round(mean(ga_values), 3),
            over25_rate=round(overs / n, 3),
            btts_rate=round(btts / n, 3),
            clean_sheet_rate=round(clean / n, 3),
            failed_to_score_rate=round(fts / n, 3),
        )

    @staticmethod
    def _latest_standing(fixture: Fixture, team: Team) -> tuple[int | None, float | None]:
        if fixture.competition_ref_id is None:
            return None, None
        row = (
            StandingSnapshot.objects.filter(competition_id=fixture.competition_ref_id, team=team)
            .filter(captured_at__lte=fixture.kickoff)
            .order_by("-captured_at")
            .first()
        )
        if not row:
            return None, None
        ppg = round(row.points / row.played, 3) if row.played else None
        return row.position, ppg

    @staticmethod
    def _lineup_player_ids(snapshot: LineupSnapshot | None) -> set[str]:
        if snapshot is None:
            return set()
        result: set[str] = set()
        for entry in snapshot.starting_xi or []:
            player = entry.get("player", entry) if isinstance(entry, dict) else {}
            player_id = player.get("id") if isinstance(player, dict) else None
            if player_id is not None:
                result.add(str(player_id))
        return result

    def _lineup_continuity(self, fixture: Fixture, team: Team) -> float | None:
        current = (
            LineupSnapshot.objects.filter(fixture=fixture, team=team)
            .order_by("-captured_at")
            .first()
        )
        previous = (
            LineupSnapshot.objects.filter(team=team, fixture__kickoff__lt=fixture.kickoff)
            .order_by("-fixture__kickoff", "-captured_at")
            .first()
        )
        current_ids = self._lineup_player_ids(current)
        previous_ids = self._lineup_player_ids(previous)
        if not current_ids or not previous_ids:
            return None
        denominator = max(1, min(11, len(current_ids), len(previous_ids)))
        return round(len(current_ids & previous_ids) / denominator, 3)

    @staticmethod
    def _latest_market_odds(fixture: Fixture, market: str, selection: str) -> float | None:
        quote = (
            OddsSnapshot.objects.filter(fixture=fixture, market=market, selection=selection)
            .order_by("-captured_at")
            .first()
        )
        return float(quote.decimal_odds) if quote else None

    @staticmethod
    def _quality_score(
        home: VenueProfile,
        away: VenueProfile,
        home_position: int | None,
        away_position: int | None,
        home_lineup: float | None,
        away_lineup: float | None,
        btts_odds: float | None,
        over_odds: float | None,
    ) -> float:
        score = 0.0
        score += min(home.sample_size / 5, 1.0) * 25
        score += min(away.sample_size / 5, 1.0) * 25
        score += 10 if home_position is not None else 0
        score += 10 if away_position is not None else 0
        score += 5 if home_lineup is not None else 0
        score += 5 if away_lineup is not None else 0
        score += 10 if btts_odds is not None else 0
        score += 10 if over_odds is not None else 0
        return round(score, 1)

    def build(self, fixture: Fixture) -> FeatureVector:
        home = self._venue_profile(fixture.home_team, "home", fixture)
        away = self._venue_profile(fixture.away_team, "away", fixture)
        home_position, home_ppg = self._latest_standing(fixture, fixture.home_team)
        away_position, away_ppg = self._latest_standing(fixture, fixture.away_team)
        home_lineup = self._lineup_continuity(fixture, fixture.home_team)
        away_lineup = self._lineup_continuity(fixture, fixture.away_team)
        btts_odds = self._latest_market_odds(fixture, "BTTS", "YES")
        over_odds = self._latest_market_odds(fixture, "OVER_2_5", "OVER")

        quality = self._quality_score(
            home,
            away,
            home_position,
            away_position,
            home_lineup,
            away_lineup,
            btts_odds,
            over_odds,
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
