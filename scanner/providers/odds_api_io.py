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

    The provider first searches the daily event feed, then falls back to the
    official text-search endpoint for obscure/reserve fixtures. Odds requests
    are retried with progressively smaller bookmaker sets so limited plans do
    not turn one unsupported bookmaker into a total fixture failure.
    """

    base_url = "https://api.odds-api.io/v3"

    def __init__(self, api_key: str | None = None, timeout: int = 15):
        self.api_key = api_key or os.getenv("ODDS_API_IO_KEY", "")
        self.timeout = timeout
        raw_bookmakers = os.getenv(
            "ODDS_API_IO_BOOKMAKERS",
            "Betano,Bet365,Unibet,Betfair,1xBet,Betway",
        )
        self.bookmaker_names = [name.strip() for name in raw_bookmakers.split(",") if name.strip()]
        self.session = requests.Session()
        self._events_cache: dict[str, list[dict]] = {}
        self._search_cache: dict[str, list[dict]] = {}
        self.last_meta: dict[str, Any] = {}

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @staticmethod
    def _normalize(value: Any) -> str:
        text = unicodedata.normalize("NFKD", str(value or ""))
        text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
        text = re.sub(r"\b(fc|cf|sc|afc|club|fk|ii|b|w)\b", " ", text)
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
        containment = 1.0 if a in b or b in a else 0.0
        return max(seq, token, containment * 0.92)

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        if not self.configured:
            return None
        query = dict(params)
        query["apiKey"] = self.api_key
        response = self.session.get(
            f"{self.base_url}/{path.lstrip('/')}",
            params=query,
            timeout=self.timeout,
        )
        self.last_meta = {
            "path": path,
            "status_code": response.status_code,
            "remaining": response.headers.get("x-ratelimit-remaining") or response.headers.get("x-ratelimit-requests-remaining"),
        }
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
            },
        )
        events = rows if isinstance(rows, list) else []
        self._events_cache[cache_key] = events
        return events

    def _search_events(self, query: str) -> list[dict]:
        key = self._normalize(query)
        if len(key) < 3:
            return []
        cached = self._search_cache.get(key)
        if cached is not None:
            return cached
        try:
            rows = self._get("events/search", {"query": query})
        except requests.RequestException:
            rows = []
        events = rows if isinstance(rows, list) else []
        self._search_cache[key] = events
        return events

    def _candidate_events(self, home: str, away: str, kickoff: datetime) -> list[dict]:
        events = list(self._events_for_kickoff(kickoff))
        # Reserve/youth/lower-division fixtures are often easier to find through
        # text search than through a broad daily feed. Search both team names and
        # de-duplicate by Odds-API event id.
        if not events or not any(
            self._similarity(home, event.get("home")) >= 0.55
            and self._similarity(away, event.get("away")) >= 0.55
            for event in events
        ):
            events.extend(self._search_events(home))
            events.extend(self._search_events(away))
        unique: dict[str, dict] = {}
        for event in events:
            event_id = event.get("id")
            if event_id is not None:
                unique[str(event_id)] = event
        return list(unique.values())

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
        for event in self._candidate_events(home, away, kickoff):
            event_time = self._event_time(event)
            if event_time is None:
                continue
            time_delta_hours = abs((event_time - kickoff.astimezone(dt_timezone.utc)).total_seconds()) / 3600.0
            if time_delta_hours > 6.0:
                continue
            home_sim = self._similarity(home, event.get("home"))
            away_sim = self._similarity(away, event.get("away"))
            reversed_home = self._similarity(home, event.get("away"))
            reversed_away = self._similarity(away, event.get("home"))
            direct = (home_sim + away_sim) / 2.0
            reversed_score = (reversed_home + reversed_away) / 2.0
            score = direct - min(time_delta_hours, 6.0) * 0.012
            if reversed_score > direct:
                continue
            if min(home_sim, away_sim) < 0.48 or direct < 0.62:
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
                if normalized == "totals" or ("total" in normalized and "team" not in normalized and "ht" not in normalized and "2h" not in normalized):
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

    def _odds_payload(self, event_id: Any) -> dict | None:
        if not self.bookmaker_names:
            return None
        attempts: list[list[str]] = []
        # Full configured set first. If the subscription restricts the number of
        # books per request, retry with two and then one without killing fixture coverage.
        attempts.append(self.bookmaker_names[:30])
        if len(self.bookmaker_names) > 2:
            attempts.append(self.bookmaker_names[:2])
        attempts.extend([[name] for name in self.bookmaker_names[:4]])

        seen: set[tuple[str, ...]] = set()
        best_payload: dict | None = None
        best_market_count = -1
        last_error: Exception | None = None
        for names in attempts:
            key = tuple(names)
            if not names or key in seen:
                continue
            seen.add(key)
            try:
                payload = self._get(
                    "odds",
                    {"eventId": str(event_id), "bookmakers": ",".join(names)},
                )
            except requests.RequestException as exc:
                last_error = exc
                continue
            if not isinstance(payload, dict):
                continue
            market_count = len(self._api_football_bookmakers(payload))
            if market_count > best_market_count:
                best_payload = payload
                best_market_count = market_count
            if market_count > 0:
                break
        if best_payload is None and last_error is not None:
            raise last_error
        return best_payload

    def fixture_odds_as_api_football(self, fixture_row: dict) -> list[dict]:
        if not self.configured:
            return []
        event = self._match_event(fixture_row)
        if not event or event.get("id") is None:
            self.last_meta = {**self.last_meta, "match": "not_found"}
            return []
        payload = self._odds_payload(event.get("id"))
        if not isinstance(payload, dict):
            return []
        bookmakers = self._api_football_bookmakers(payload)
        self.last_meta = {
            **self.last_meta,
            "match": "found",
            "event_id": event.get("id"),
            "event_home": event.get("home"),
            "event_away": event.get("away"),
            "market_bookmakers": len(bookmakers),
        }
        if not bookmakers:
            return []
        return [{
            "league": payload.get("league") or {},
            "fixture": {"id": event.get("id")},
            "bookmakers": bookmakers,
        }]
