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


FIXTURE_UPDATE_FIELDS = [
    "competition", "competition_ref", "season", "round", "kickoff",
    "home_team", "away_team", "venue", "venue_city", "referee",
    "status", "home_goals", "away_goals",
]


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

    @staticmethod
    def _fixture_signature(fixture: Fixture) -> tuple:
        return (
            fixture.competition,
            fixture.competition_ref_id,
            fixture.season,
            fixture.round,
            fixture.kickoff,
            fixture.home_team_id,
            fixture.away_team_id,
            fixture.venue,
            fixture.venue_city,
            fixture.referee,
            fixture.status,
            fixture.home_goals,
            fixture.away_goals,
        )

    def _bulk_ingest_fixtures(self, raw_fixtures: list[dict]) -> tuple[list[Fixture], dict[str, int]]:
        """Fast path: create new fixtures and update only rows whose payload changed."""
        if not raw_fixtures:
            return [], {"created": 0, "changed": 0, "unchanged": 0}

        team_raw_by_id: dict[str, dict] = {}
        comp_raw_by_key: dict[tuple[str, int | None], dict] = {}
        fixture_external_ids: list[str] = []

        for raw in raw_fixtures:
            teams = raw.get("teams") or {}
            for side in ("home", "away"):
                team_raw = teams.get(side) or {}
                if team_raw.get("id") is not None:
                    team_raw_by_id[str(team_raw.get("id"))] = team_raw
            league = raw.get("league") or {}
            if league.get("id") is not None:
                comp_raw_by_key[(str(league.get("id")), league.get("season"))] = league
            fixture_meta = raw.get("fixture") or {}
            if fixture_meta.get("id") is not None:
                fixture_external_ids.append(str(fixture_meta.get("id")))

        self._log(
            f"[ingest] bulk prepare teams={len(team_raw_by_id)} competitions={len(comp_raw_by_key)} "
            f"fixtures={len(fixture_external_ids)}"
        )

        with transaction.atomic():
            existing_teams = {
                team.external_id: team
                for team in Team.objects.filter(external_id__in=list(team_raw_by_id)).iterator(chunk_size=2000)
            }
            missing_teams = [
                Team(
                    external_id=external_id,
                    name=raw.get("name") or "Unknown",
                    country=raw.get("country") or "",
                    logo=raw.get("logo") or "",
                )
                for external_id, raw in team_raw_by_id.items()
                if external_id not in existing_teams
            ]
            if missing_teams:
                Team.objects.bulk_create(missing_teams, batch_size=1000, ignore_conflicts=True)
            team_map = {
                team.external_id: team
                for team in Team.objects.filter(external_id__in=list(team_raw_by_id)).iterator(chunk_size=2000)
            }
            self._team_cache.update(team_map)

            competition_ids = {external_id for external_id, _season in comp_raw_by_key}
            existing_competitions = {
                (comp.external_id, comp.season): comp
                for comp in Competition.objects.filter(external_id__in=list(competition_ids)).iterator(chunk_size=1000)
            }
            missing_competitions = []
            for key, league in comp_raw_by_key.items():
                if key in existing_competitions:
                    continue
                missing_competitions.append(
                    Competition(
                        external_id=key[0],
                        season=key[1],
                        name=league.get("name") or "Unknown",
                        country=league.get("country") or "",
                        competition_type=league.get("type") or "",
                        logo=league.get("logo") or "",
                    )
                )
            if missing_competitions:
                Competition.objects.bulk_create(missing_competitions, batch_size=500, ignore_conflicts=True)
            competition_map = {
                (comp.external_id, comp.season): comp
                for comp in Competition.objects.filter(external_id__in=list(competition_ids)).iterator(chunk_size=1000)
            }
            self._competition_cache.update(competition_map)

            existing_fixtures = {
                fixture.external_id: fixture
                for fixture in Fixture.objects.filter(external_id__in=fixture_external_ids).iterator(chunk_size=2000)
            }
            to_create: list[Fixture] = []
            to_update: list[Fixture] = []
            unchanged = 0

            for raw in raw_fixtures:
                teams = raw.get("teams") or {}
                fixture_meta = raw.get("fixture") or {}
                league = raw.get("league") or {}
                goals = raw.get("goals") or {}
                status = fixture_meta.get("status") or {}
                venue = fixture_meta.get("venue") or {}
                external_id = str(fixture_meta.get("id"))
                home_raw = teams.get("home") or {}
                away_raw = teams.get("away") or {}
                home = team_map.get(str(home_raw.get("id")))
                away = team_map.get(str(away_raw.get("id")))
                competition = competition_map.get((str(league.get("id")), league.get("season")))
                if not home or not away:
                    continue

                desired = Fixture(
                    external_id=external_id,
                    competition=league.get("name") or "Unknown",
                    competition_ref=competition,
                    season=league.get("season"),
                    round=league.get("round") or "",
                    kickoff=self._kickoff(raw),
                    home_team=home,
                    away_team=away,
                    venue=venue.get("name") or "",
                    venue_city=venue.get("city") or "",
                    referee=fixture_meta.get("referee") or "",
                    status=status.get("short") or "scheduled",
                    home_goals=goals.get("home"),
                    away_goals=goals.get("away"),
                )
                current = existing_fixtures.get(external_id)
                if current is None:
                    to_create.append(desired)
                    continue

                desired.id = current.id
                if self._fixture_signature(current) == self._fixture_signature(desired):
                    unchanged += 1
                    continue

                for field in FIXTURE_UPDATE_FIELDS:
                    if field.endswith("_ref"):
                        setattr(current, field, getattr(desired, field))
                    else:
                        setattr(current, field, getattr(desired, field))
                to_update.append(current)

            if to_create:
                Fixture.objects.bulk_create(to_create, batch_size=1000, ignore_conflicts=True)
            if to_update:
                Fixture.objects.bulk_update(to_update, FIXTURE_UPDATE_FIELDS, batch_size=1000)

        fixtures = list(
            Fixture.objects.filter(external_id__in=fixture_external_ids)
            .select_related("home_team", "away_team", "competition_ref")
        )
        stats = {"created": len(to_create), "changed": len(to_update), "unchanged": unchanged}
        self._log(
            f"[ingest] delta created={stats['created']} changed={stats['changed']} "
            f"unchanged={stats['unchanged']}"
        )
        self._log(f"[ingest] bulk persisted {len(fixtures)}/{len(raw_fixtures)} fixtures")
        return fixtures, stats

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

        errors: list[dict] = []
        lineups = 0
        statistics = 0
        standings = 0
        fixture_delta = {"created": 0, "changed": 0, "unchanged": 0}

        if not include_details:
            fixtures, fixture_delta = self._bulk_ingest_fixtures(raw_fixtures)
        else:
            fixtures: list[Fixture] = []
            total = len(raw_fixtures)
            for index, raw in enumerate(raw_fixtures, start=1):
                try:
                    fixture = self.upsert_fixture(raw)
                    fixtures.append(fixture)
                    lineups += self.ingest_lineups(fixture)
                    if fixture.status in {"FT", "AET", "PEN"}:
                        statistics += self.ingest_statistics(fixture)
                except Exception as exc:
                    errors.append({"fixture_id": ((raw.get("fixture") or {}).get("id")), "error": str(exc)})
                if index == 1 or index % 100 == 0 or index == total:
                    self._log(f"[ingest] detailed persisted {index}/{total}; errors={len(errors)}")

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
            "fixtures_created": fixture_delta["created"],
            "fixtures_changed": fixture_delta["changed"],
            "fixtures_unchanged": fixture_delta["unchanged"],
            "lineups_created": lineups,
            "statistics_created": statistics,
            "standings_created": standings,
            "errors": errors,
        }


def team_id(team: Team) -> str:
    return str(team.external_id)
