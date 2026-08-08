from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable

from django.db import transaction
from django.utils import timezone

from engine.models import (
    Competition,
    Fixture,
    LineupSnapshot,
    StandingSnapshot,
    Team,
    TeamStatisticsSnapshot,
)

from .providers.base import SportsDataProvider


class DataIngestionService:
    def __init__(self, provider: SportsDataProvider, *, progress: Callable[[str], None] | None = None):
        self.provider = provider
        self.progress = progress or (lambda _message: None)
        self._team_cache: dict[str, Team] = {}
        self._competition_cache: dict[tuple[str, int | None], Competition] = {}

    def _log(self, message: str) -> None:
        self.progress(message)

    @staticmethod
    def _kickoff(raw: dict) -> datetime:
        value = ((raw.get("fixture") or {}).get("date"))
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if timezone.is_aware(dt) else timezone.make_aware(dt)

    def _upsert_team(self, raw: dict) -> Team:
        external_id = str(raw.get("id"))
        cached = self._team_cache.get(external_id)
        if cached is not None:
            return cached
        team = Team.objects.update_or_create(
            external_id=external_id,
            defaults={
                "name": raw.get("name") or "Unknown",
                "country": raw.get("country") or "",
                "logo": raw.get("logo") or "",
            },
        )[0]
        self._team_cache[external_id] = team
        return team

    def _upsert_competition(self, raw_fixture: dict) -> Competition:
        league = raw_fixture.get("league") or {}
        external_id = str(league.get("id"))
        season = league.get("season")
        key = (external_id, season)
        cached = self._competition_cache.get(key)
        if cached is not None:
            return cached
        competition = Competition.objects.update_or_create(
            external_id=external_id,
            season=season,
            defaults={
                "name": league.get("name") or "Unknown",
                "country": league.get("country") or "",
                "competition_type": league.get("type") or "",
                "logo": league.get("logo") or "",
            },
        )[0]
        self._competition_cache[key] = competition
        return competition

    def upsert_fixture(self, raw: dict) -> Fixture:
        teams = raw.get("teams") or {}
        fixture_meta = raw.get("fixture") or {}
        league = raw.get("league") or {}
        goals = raw.get("goals") or {}
        status = fixture_meta.get("status") or {}
        venue = fixture_meta.get("venue") or {}

        home = self._upsert_team(teams.get("home") or {})
        away = self._upsert_team(teams.get("away") or {})
        competition = self._upsert_competition(raw)

        return Fixture.objects.update_or_create(
            external_id=str(fixture_meta.get("id")),
            defaults={
                "competition": league.get("name") or "Unknown",
                "competition_ref": competition,
                "season": league.get("season"),
                "round": league.get("round") or "",
                "kickoff": self._kickoff(raw),
                "home_team": home,
                "away_team": away,
                "venue": venue.get("name") or "",
                "venue_city": venue.get("city") or "",
                "referee": fixture_meta.get("referee") or "",
                "status": status.get("short") or "scheduled",
                "home_goals": goals.get("home"),
                "away_goals": goals.get("away"),
            },
        )[0]

    def ingest_lineups(self, fixture: Fixture) -> int:
        payload = self.provider.fixture_lineups(fixture.external_id)
        count = 0
        for row in payload:
            team_raw = row.get("team") or {}
            team = self._upsert_team(team_raw)
            coach = row.get("coach") or {}
            start_xi = row.get("startXI") or []
            substitutes = row.get("substitutes") or []

            latest = LineupSnapshot.objects.filter(fixture=fixture, team=team).order_by("-captured_at").first()
            normalized_xi = [item.get("player") or {} for item in start_xi]
            normalized_subs = [item.get("player") or {} for item in substitutes]

            if latest and latest.starting_xi == normalized_xi and latest.substitutes == normalized_subs:
                continue

            LineupSnapshot.objects.create(
                fixture=fixture,
                team=team,
                formation=row.get("formation") or "",
                coach_name=coach.get("name") or "",
                starting_xi=normalized_xi,
                substitutes=normalized_subs,
            )
            count += 1
        return count

    def ingest_statistics(self, fixture: Fixture) -> int:
        payload = self.provider.fixture_statistics(fixture.external_id)
        count = 0
        for row in payload:
            team_raw = row.get("team") or {}
            team = self._upsert_team(team_raw)
            stats: dict[str, Any] = {}
            for item in row.get("statistics") or []:
                key = str(item.get("type") or "").strip()
                if key:
                    stats[key] = item.get("value")

            TeamStatisticsSnapshot.objects.create(
                fixture=fixture,
                team=team,
                is_home=team_id(team) == team_id(fixture.home_team),
                statistics=stats,
            )
            count += 1
        return count

    def ingest_standings(self, competition: Competition) -> int:
        if not competition.season:
            return 0
        payload = self.provider.standings(competition.external_id, competition.season)
        count = 0
        for response in payload:
            league = response.get("league") or {}
            groups = league.get("standings") or []
            for group in groups:
                for row in group:
                    team = self._upsert_team(row.get("team") or {})
                    all_stats = row.get("all") or {}
                    goals = all_stats.get("goals") or {}
                    StandingSnapshot.objects.create(
                        competition=competition,
                        team=team,
                        position=row.get("rank") or 0,
                        played=all_stats.get("played") or 0,
                        won=all_stats.get("win") or 0,
                        draw=all_stats.get("draw") or 0,
                        lost=all_stats.get("lose") or 0,
                        goals_for=goals.get("for") or 0,
                        goals_against=goals.get("against") or 0,
                        points=row.get("points") or 0,
                        form=row.get("form") or "",
                    )
                    count += 1
        return count

    def ingest_date(self, target_date: date, *, include_details: bool = True) -> dict:
        self._log(f"[ingest] requesting fixtures for {target_date.isoformat()}")
        raw_fixtures = self.provider.fixtures_by_date(target_date)
        self._log(f"[ingest] API returned {len(raw_fixtures)} fixtures")

        fixtures: list[Fixture] = []
        errors: list[dict] = []
        lineups = 0
        statistics = 0
        standings = 0
        total = len(raw_fixtures)

        # Keep the fixture discovery write phase in one transaction and reuse
        # cached Team/Competition instances. This matters when GitHub Actions is
        # talking to Render PostgreSQL over an external connection.
        with transaction.atomic():
            for index, raw in enumerate(raw_fixtures, start=1):
                try:
                    fixture = self.upsert_fixture(raw)
                    fixtures.append(fixture)
                    if include_details:
                        lineups += self.ingest_lineups(fixture)
                        if fixture.status in {"FT", "AET", "PEN"}:
                            statistics += self.ingest_statistics(fixture)
                except Exception as exc:
                    errors.append({"fixture_id": ((raw.get("fixture") or {}).get("id")), "error": str(exc)})

                if index == 1 or index % 100 == 0 or index == total:
                    self._log(f"[ingest] persisted {index}/{total} fixtures; errors={len(errors)}")

        # A fixtures-only morning scan must remain cheap. Standings require one
        # API request per competition and belong to the detailed enrichment path.
        if include_details:
            competitions_seen: set[int] = set()
            for fixture in fixtures:
                competition = fixture.competition_ref
                if not competition or competition.id in competitions_seen:
                    continue
                competitions_seen.add(competition.id)
                try:
                    standings += self.ingest_standings(competition)
                except Exception as exc:
                    errors.append({"competition_id": competition.external_id, "error": str(exc)})

        self._log(
            f"[ingest] complete fixtures={len(fixtures)} lineups={lineups} "
            f"statistics={statistics} standings={standings} errors={len(errors)}"
        )
        return {
            "date": target_date.isoformat(),
            "fixtures": len(fixtures),
            "lineups_created": lineups,
            "statistics_created": statistics,
            "standings_created": standings,
            "errors": errors,
        }


def team_id(team: Team) -> str:
    return str(team.external_id)
