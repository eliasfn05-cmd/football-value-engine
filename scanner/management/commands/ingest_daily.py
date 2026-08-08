from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError

from scanner.ingestion import DataIngestionService
from scanner.providers.api_football import APIFootballProvider


class Command(BaseCommand):
    help = "Ingest fixtures and available structured data from API-Football into PostgreSQL."

    def add_arguments(self, parser):
        parser.add_argument("--date", dest="target_date", help="YYYY-MM-DD. Defaults to America/Lima today.")
        parser.add_argument("--fixtures-only", action="store_true", help="Only persist fixtures/teams/competitions.")

    def handle(self, *args, **options):
        raw_date = options.get("target_date")
        try:
            target_date = date.fromisoformat(raw_date) if raw_date else datetime.now(ZoneInfo("America/Lima")).date()
        except ValueError as exc:
            raise CommandError("--date must use YYYY-MM-DD") from exc

        try:
            service = DataIngestionService(
                APIFootballProvider(),
                progress=lambda message: self.stdout.write(message),
            )
            report = service.ingest_date(
                target_date,
                include_details=not options.get("fixtures_only"),
            )
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        self.stdout.write(self.style.SUCCESS(
            f"Ingested {report['fixtures']} fixtures for {report['date']} with {len(report['errors'])} errors."
        ))
