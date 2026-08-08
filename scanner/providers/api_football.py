from __future__ import annotations

import os
from datetime import date

import requests

from .base import SportsDataProvider


class APIFootballProvider(SportsDataProvider):
    base_url = "https://v3.football.api-sports.io"

    def __init__(self, api_key: str | None = None, timeout: int = 20):
        self.api_key = api_key or os.getenv("API_FOOTBALL_KEY", "")
        if not self.api_key:
            raise RuntimeError("API_FOOTBALL_KEY is not configured")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"x-apisports-key": self.api_key})

    def _get(self, endpoint: str, params: dict) -> list[dict]:
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
        return payload.get("response", [])

    def fixtures_by_date(self, target_date: date) -> list[dict]:
        return self._get("fixtures", {"date": target_date.isoformat()})

    def team_recent_fixtures(self, team_id: int | str, *, last: int = 10) -> list[dict]:
        return self._get("fixtures", {"team": team_id, "last": last, "status": "FT"})

    def head_to_head(self, home_team_id: int | str, away_team_id: int | str, *, last: int = 5) -> list[dict]:
        return self._get("fixtures/headtohead", {"h2h": f"{home_team_id}-{away_team_id}", "last": last})

    def fixture_odds(self, fixture_id: int | str) -> list[dict]:
        return self._get("odds", {"fixture": fixture_id})

    def fixture_lineups(self, fixture_id: int | str) -> list[dict]:
        return self._get("fixtures/lineups", {"fixture": fixture_id})
