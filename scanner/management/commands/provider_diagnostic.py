from __future__ import annotations

import json
import os
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from scanner.odds import parse_quotes
from scanner.providers.api_football import APIFootballProvider


class Command(BaseCommand):
    help = "Validate API-Football connectivity, Betano availability and market coverage."

    def add_arguments(self, parser):
        parser.add_argument("--date", dest="target_date", help="YYYY-MM-DD. Defaults to today.")
        parser.add_argument("--fixture-id", dest="fixture_id", help="Optional fixture id to inspect odds/lineups.")

    def handle(self, *args, **options):
        try:
            target_date = date.fromisoformat(options["target_date"]) if options.get("target_date") else date.today()
            provider = APIFootballProvider()
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        preferred = os.getenv("PREFERRED_BOOKMAKER", "Betano")
        report = {
            "provider": "API-Football",
            "date": target_date.isoformat(),
            "preferred_bookmaker": preferred,
            "api_key_configured": True,
        }

        try:
            status = provider.account_status()
            report["account_status"] = status
        except Exception as exc:
            report["account_status_error"] = str(exc)

        try:
            bookmakers = provider.bookmakers()
            names = [str(item.get("name", "")) for item in bookmakers]
            report["bookmakers_count"] = len(names)
            report["preferred_bookmaker_available"] = any(name.casefold() == preferred.casefold() for name in names)
            report["preferred_bookmaker_matches"] = [name for name in names if preferred.casefold() in name.casefold()]
        except Exception as exc:
            report["bookmakers_error"] = str(exc)

        try:
            fixtures = provider.fixtures_by_date(target_date)
            report["fixtures_available"] = len(fixtures)
            report["sample_fixtures"] = [
                {
                    "fixture_id": (item.get("fixture") or {}).get("id"),
                    "league": (item.get("league") or {}).get("name"),
                    "home": ((item.get("teams") or {}).get("home") or {}).get("name"),
                    "away": ((item.get("teams") or {}).get("away") or {}).get("name"),
                }
                for item in fixtures[:10]
            ]
        except Exception as exc:
            report["fixtures_error"] = str(exc)

        fixture_id = options.get("fixture_id")
        if fixture_id:
            try:
                odds = provider.fixture_odds(fixture_id)
                quotes = parse_quotes(odds)
                report["fixture_odds"] = {
                    "btts": vars(quotes["btts"]) if quotes.get("btts") else None,
                    "over25": vars(quotes["over25"]) if quotes.get("over25") else None,
                }
            except Exception as exc:
                report["fixture_odds_error"] = str(exc)

            try:
                lineups = provider.fixture_lineups(fixture_id)
                report["fixture_lineups_available"] = bool(lineups)
                report["fixture_lineup_teams"] = [((item.get("team") or {}).get("name")) for item in lineups]
            except Exception as exc:
                report["fixture_lineups_error"] = str(exc)

        report["last_request_meta"] = provider.last_request_meta
        self.stdout.write(json.dumps(report, indent=2, ensure_ascii=False, default=str))
