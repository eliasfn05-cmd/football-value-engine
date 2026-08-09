from __future__ import annotations

import os
import re
import unicodedata
from datetime import date
from typing import Any

import requests

from .base import SportsDataProvider
from .odds_api_io import OddsApiIoProvider


FRIENDLY_TERMS = (
    "friendly",
    "friendlies",
    "friendly games",
    "club friendly",
    "club friendlies",
    "friendlies clubs",
    "international friendly",
    "international friendlies",
    "amistoso",
    "amistosos",
    "exhibition",
    "exhibicion",
    "test match",
    "preseason",
    "pre season",
    "pretemporada",
    "training match",
    "legends match",
    "legend match",
    "all star",
    "soccer aid",
    "charity match",
    "benefit match",
)


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


_NORMALIZED_FRIENDLY_TERMS = tuple(_normalize(term) for term in FRIENDLY_TERMS)


class APIFootballProvider(SportsDataProvider):
    base_url = "https://v3.football.api-sports.io"

    def __init__(self, api_key: str | None = None, timeout: int = 20):
        self.api_key = api_key or os.getenv("API_FOOTBALL_KEY", "")
        if not self.api_key:
            raise RuntimeError("API_FOOTBALL_KEY is not configured")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"x-apisports-key": self.api_key})
        self.last_request_meta: dict[str, Any] = {}
        self.secondary_odds = OddsApiIoProvider(timeout=min(timeout, 15))

    def _get_payload(self, endpoint: str, params: dict) -> dict:
        response = self.session.get(
            f"{self.base_url}/{endpoint.lstrip('/')}",
            params=params,
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        errors = payload.get("errors") or {}
        if errors:
            raise RuntimeError(f"API-Football error: {errors}")

        self.last_request_meta = {
            "endpoint": endpoint,
            "results": payload.get("results"),
            "paging": payload.get("paging") or {},
            "rate_limit_remaining": response.headers.get("x-ratelimit-requests-remaining"),
            "rate_limit_limit": response.headers.get("x-ratelimit-requests-limit"),
        }
        return payload

    def _get(self, endpoint: str, params: dict) -> list[dict]:
        return self._get_payload(endpoint, params).get("response", [])

    @staticmethod
    def _fixture_competition_text(row: dict) -> str:
        league = row.get("league") or {}
        fixture = row.get("fixture") or {}
        status = fixture.get("status") or {}
        return _normalize(
            " ".join(
                str(value or "")
                for value in (
                    league.get("id"),
                    league.get("name"),
                    league.get("type"),
                    league.get("country"),
                    league.get("round"),
                    status.get("long"),
                )
            )
        )

    @classmethod
    def is_friendly_fixture(cls, row: dict) -> bool:
        text = cls._fixture_competition_text(row)
        return any(term and term in text for term in _NORMALIZED_FRIENDLY_TERMS)

    def _official_fixtures_only(self, rows: list[dict]) -> list[dict]:
        official = [row for row in rows if not self.is_friendly_fixture(row)]
        excluded = len(rows) - len(official)
        if excluded:
            self.last_request_meta["friendlies_excluded"] = excluded
        return official

    @staticmethod
    def _goal_market_coverage(payload: list[dict]) -> tuple[bool, bool]:
        has_btts = False
        has_over25 = False
        for response_item in payload or []:
            for bookmaker in response_item.get("bookmakers") or []:
                for bet in bookmaker.get("bets") or []:
                    bet_name = _normalize(bet.get("name", ""))
                    if "both teams" in bet_name and "score" in bet_name:
                        for value in bet.get("values") or []:
                            if _normalize(value.get("value", "")) in {"yes", "si"}:
                                has_btts = True
                    if "over under" in bet_name or "total goals" in bet_name or "goals over under" in bet_name:
                        for value in bet.get("values") or []:
                            compact = _normalize(value.get("value", "")).replace(" ", "")
                            if compact in {"over25", "25"} or "over25" in compact:
                                has_over25 = True
                    if has_btts and has_over25:
                        return True, True
        return has_btts, has_over25

    def account_status(self) -> list[dict]:
        return self._get("status", {})

    def bookmakers(self) -> list[dict]:
        return self._get("odds/bookmakers", {})

    def fixtures_by_date(self, target_date: date) -> list[dict]:
        rows = self._get("fixtures", {"date": target_date.isoformat(), "timezone": "America/Lima"})
        return self._official_fixtures_only(rows)

    def team_recent_fixtures(self, team_id: int | str, *, last: int = 10) -> list[dict]:
        rows = self._get("fixtures", {"team": team_id, "last": last, "status": "FT"})
        return self._official_fixtures_only(rows)

    def team_fixtures_between(self, team_id: int | str, start: date, end: date) -> list[dict]:
        rows = self._get(
            "fixtures",
            {"team": team_id, "from": start.isoformat(), "to": end.isoformat()},
        )
        return self._official_fixtures_only(rows)

    def head_to_head(self, home_team_id: int | str, away_team_id: int | str, *, last: int = 5) -> list[dict]:
        rows = self._get("fixtures/headtohead", {"h2h": f"{home_team_id}-{away_team_id}", "last": last})
        return self._official_fixtures_only(rows)

    def fixture_odds(self, fixture_id: int | str) -> list[dict]:
        primary = self._get("odds", {"fixture": fixture_id})
        has_btts, has_over25 = self._goal_market_coverage(primary)
        if (has_btts and has_over25) or not self.secondary_odds.configured:
            self.last_request_meta["odds_secondary_used"] = False
            self.last_request_meta["odds_primary_btts"] = has_btts
            self.last_request_meta["odds_primary_over25"] = has_over25
            return primary

        fixture_rows = self._get("fixtures", {"id": fixture_id})
        secondary: list[dict] = []
        if fixture_rows:
            try:
                secondary = self.secondary_odds.fixture_odds_as_api_football(fixture_rows[0])
            except Exception as exc:
                self.last_request_meta["odds_secondary_error"] = str(exc)[:180]

        secondary_btts, secondary_over25 = self._goal_market_coverage(secondary)
        self.last_request_meta["odds_secondary_used"] = bool(secondary)
        self.last_request_meta["odds_primary_btts"] = has_btts
        self.last_request_meta["odds_primary_over25"] = has_over25
        self.last_request_meta["odds_secondary_btts"] = secondary_btts
        self.last_request_meta["odds_secondary_over25"] = secondary_over25
        return list(primary or []) + list(secondary or [])

    def fixture_lineups(self, fixture_id: int | str) -> list[dict]:
        return self._get("fixtures/lineups", {"fixture": fixture_id})

    def fixture_statistics(self, fixture_id: int | str) -> list[dict]:
        return self._get("fixtures/statistics", {"fixture": fixture_id})

    def standings(self, league_id: int | str, season: int | str) -> list[dict]:
        return self._get("standings", {"league": league_id, "season": season})
