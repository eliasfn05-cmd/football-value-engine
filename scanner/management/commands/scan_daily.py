from __future__ import annotations

import json
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from scanner.providers.api_football import APIFootballProvider
from scanner.service import DailyScanner


class Command(BaseCommand):
    help = "Scan one date, persist predictions and print the Tier A board ordered by EV."

    def add_arguments(self, parser):
        parser.add_argument("--date", dest="target_date", help="YYYY-MM-DD. Defaults to today.")

    def handle(self, *args, **options):
        raw_date = options.get("target_date")
        try:
            target_date = date.fromisoformat(raw_date) if raw_date else date.today()
        except ValueError as exc:
            raise CommandError("--date must use YYYY-MM-DD") from exc

        try:
            provider = APIFootballProvider()
            report = DailyScanner(provider).scan_date(target_date)
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        self.stdout.write(self.style.SUCCESS(
            f"Scanned {report['fixtures_scanned']} fixtures; Tier A selections: {len(report['tier_a'])}"
        ))
