from __future__ import annotations

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError

from scanner.providers.api_football import APIFootballProvider
from scanner.service import DailyScanner


class Command(BaseCommand):
    help = "Scan one date, persist predictions and print the Tier A board ordered by EV."

    def add_arguments(self, parser):
        parser.add_argument("--date", dest="target_date", help="YYYY-MM-DD. Defaults to APP_TIMEZONE local date.")

    def handle(self, *args, **options):
        timezone_name = os.getenv("APP_TIMEZONE", "America/Lima")
        raw_date = options.get("target_date")
        try:
            target_date = datetime.fromisoformat(raw_date).date() if raw_date else datetime.now(ZoneInfo(timezone_name)).date()
        except (ValueError, KeyError) as exc:
            raise CommandError("Invalid --date or APP_TIMEZONE") from exc

        try:
            provider = APIFootballProvider()
            report = DailyScanner(provider).scan_date(target_date)
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        report["app_timezone"] = timezone_name
        self.stdout.write(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        self.stdout.write(self.style.SUCCESS(
            f"Scanned {report['fixtures_scanned']} fixtures; Betano coverage {report['coverage_betano_pct']}%; Tier A selections: {len(report['tier_a'])}"
        ))
