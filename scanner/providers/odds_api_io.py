from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone as dt_timezone
from difflib import SequenceMatcher
from typing import Any

import requests


class OddsApiIoProvider:
    """Secondary real-bookmaker odds source for fixtures missing API-Football prices.

    Odds-API.io is queried only as a fallback. Responses are converted to the
    API-Football odds shape so the existing quote parser and OddsSnapshot logic
    remain the single source of truth downstream.
    """

    base_url = "https://api.odds-api.io/v3"

    def __init__(self, api_key: str | None = None, timeout: int = 15):
        self.api_key = api_key or os.getenv("ODDS_API_IO_KEY", "")
        self.timeout = timeout
        self.bookmakers = os.getenv(
            "ODDS_API_IO_BOOKMAKERS",
            "Betano,Bet365,Betfair,Unibet,1xBet,Betway",
        )
        self.session = requests.Session()
        self._events_cache: dict[str, list[dict]] = {}

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @staticmethod
    def _normalize(value: Any) -> str:
        text = unicodedata.normalize("NFKD", str(value or ""))
        text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
        text = re.sub(r"\b(fc|cf|sc|afc|club|fk)\b", " ", text)
        text = re.sub(r"[^a-z0-9]+", " ", text)
        return " ".join(text.split())

    @classmethod
    def _similarity(cls, left: Any, right: Any) -> float:
        a, b = cls._normalize(left), cls._normalize(right)
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        seq = SequenceMatcher(None, a, b).ratio()
        aset, bset = set(a.split()), set(b.split())
        token = len(aset & bset) / max(1, len(aset | bset))
        return max(seq, token)

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        if not self.configured:
            return None
        query = dict(params)
        query["apiKey"] = self.api_key
        response = self.session.get(f"{self.base_url}/{path.lstrip('/')}", params=query, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _event_time(raw: dict) -> datetime | None:
        value = raw.get("date")
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_timezone.utc)

    def _events_for_kickoff(self, kickoff: datetime) -> list[dict]:
        utc = kickoff.astimezone(dt_timezone.utc)
        cache_key = utc.date().isoformat()
        cached = self._events_cache.get(cache_key)
        if cached is not None:
            return cached
        start = datetime.combine(utc.date(), datetime.min.time(), tzinfo=dt_timezone.utc) - timedelta(hours=6)
        end = start + timedelta(hours=36)
        rows = self._get(
            "events",
            {
                "sport": "football",
                "status": "pending,live",
                "from": start.isoformat().replace("+00:00", "Z"),
                "to": end.isoformat().replace("+00:00", "Z"),
                "limit": 5000,
            },
        )
        events = rows if isinstance(rows, list) else []
        self._events_cache[cache_key] = events
        return events

    def _match_event(self, fixture_row: dict) -> dict | None:
        teams = fixture_row.get("teams") or {}
        home = (teams.get("home") or {}).get("name") or ""
        away = (teams.get("away") or {}).get("name") or ""
        date_text = (fixture_row.get("fixture") or {}).get("date")
        if not date_text:
            return None
        try:
            kickoff = datetime.fromisoformat(str(date_text).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if not kickoff.tzinfo:
            kickoff = kickoff.replace(tzinfo=dt_timezone.utc)

        best: tuple[float, dict] | None = None
        for event in self._events_for_kickoff(kickoff):
            event_time = self._event_time(event)
            if event_time is None:
                continue
            time_delta_hours = abs((event_time - kickoff.astimezone(dt_timezone.utc)).total_seconds()) / 3600.0
            if time_delta_hours > 4.0:
                continue
            home_sim = self._similarity(home, event.get("home"))
            away_sim = self._similarity(away, event.get("away"))
            reversed_home = self._similarity(home, event.get("away"))
            reversed_away = self._similarity(away, event.get("home"))
            direct = (home_sim + away_sim) / 2.0
            reversed_score = (reversed_home + reversed_away) / 2.0
            score = direct - min(time_delta_hours, 4.0) * 0.015
            if reversed_score > direct:
                continue
            if min(home_sim, away_sim) < 0.55 or direct < 0.68:
                continue
            if best is None or score > best[0]:
                best = (score, event)
        return best[1] if best else None

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if result > 1.0 else None

    @classmethod
    def _api_football_bookmakers(cls, odds_payload: dict) -> list[dict]:
        output: list[dict] = []
        for bookmaker_name, markets in (odds_payload.get("bookmakers") or {}).items():
            bets: list[dict] = []
            for market in markets or []:
                name = str(market.get("name") or "").strip()
                normalized = name.lower()
                if normalized == "totals" or ("total" in normalized and "team" not in normalized and "ht" not in normalized):
                    values = []
                    for line in market.get("odds") or []:
                        handicap = line.get("max", line.get("hdp"))
                        try:
                            hdp = float(handicap)
                        except (TypeError, ValueError):
                            continue
                        if abs(hdp - 2.5) > 0.001:
                            continue
                        over = cls._number(line.get("over"))
                        under = cls._number(line.get("under"))
                        if over is not None:
                            values.append({"value": "Over 2.5", "odd": str(over)})
                        if under is not None:
                            values.append({"value": "Under 2.5", "odd": str(under)})
                    if values:
                        bets.append({"name": "Goals Over/Under", "values": values})

                if "both teams" in normalized and "score" in normalized:
                    values = []
                    for line in market.get("odds") or []:
                        yes = cls._number(line.get("yes"))
                        no = cls._number(line.get("no"))
                        # Some normalized feeds use home/away for binary Yes/No.
                        if yes is None and no is None and "draw" not in line:
                            yes = cls._number(line.get("home"))
                            no = cls._number(line.get("away"))
                        if yes is not None:
                            values.append({"value": "Yes", "odd": str(yes)})
                        if no is not None:
                            values.append({"value": "No", "odd": str(no)})
                    if values:
                        bets.append({"name": "Both Teams To Score", "values": values})
            if bets:
                output.append({"name": bookmaker_name, "bets": bets})
        return output

    def fixture_odds_as_api_football(self, fixture_row: dict) -> list[dict]:
        if not self.configured:
            return []
        event = self._match_event(fixture_row)
        if not event or event.get("id") is None:
            return []
        payload = self._get(
            "odds",
            {"eventId": str(event["id"]), "bookmakers": self.bookmakers},
        )
        if not isinstance(payload, dict):
            return []
        bookmakers = self._api_football_bookmakers(payload)
        if not bookmakers:
            return []
        return [{"league": payload.get("league") or {}, "fixture": {"id": event.get("id")}, "bookmakers": bookmakers}]
