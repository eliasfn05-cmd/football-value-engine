from __future__ import annotations

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from scanner.providers.api_football import APIFootballProvider


class Command(BaseCommand):
    help = "Validate production readiness: DB, API-Football, fixtures and bookmaker coverage."

    def add_arguments(self, parser):
        parser.add_argument("--skip-provider", action="store_true", help="Only validate local app and database readiness.")

    def handle(self, *args, **options):
        timezone_name = os.getenv("APP_TIMEZONE", "America/Lima")
        now = datetime.now(ZoneInfo(timezone_name))

        report = {
            "ok": True,
            "timestamp": now.isoformat(),
            "timezone": timezone_name,
            "database": {"ok": False},
            "provider": {"configured": bool(os.getenv("API_FOOTBALL_KEY")), "ok": None},
            "preferred_bookmaker": os.getenv("PREFERRED_BOOKMAKER", "Betano"),
        }

        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                value = cursor.fetchone()[0]
            report["database"] = {"ok": value == 1}
        except Exception as exc:
            report["ok"] = False
            report["database"] = {"ok": False, "error": str(exc)}

        if not options["skip_provider"]:
            if not report["provider"]["configured"]:
                report["ok"] = False
                report["provider"].update({"ok": False, "error": "API_FOOTBALL_KEY is not configured"})
            else:
                try:
                    provider = APIFootballProvider()
                    fixtures = provider.fixtures_by_date(now.date())
                    bookmakers = provider.bookmakers()
                    preferred = report["preferred_bookmaker"].lower()
                    matches = [b for b in bookmakers if preferred in str(b.get("name", "")).lower()]
                    report["provider"].update({
                        "ok": True,
                        "fixtures_today": len(fixtures),
                        "bookmakers_available": len(bookmakers),
                        "preferred_bookmaker_found": bool(matches),
                        "preferred_bookmaker_matches": [b.get("name") for b in matches[:10]],
                    })
                except Exception as exc:
                    report["ok"] = False
                    report["provider"].update({"ok": False, "error": str(exc)})

        self.stdout.write(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        if not report["ok"]:
            raise CommandError("Production preflight failed. Review the report above.")
        self.stdout.write(self.style.SUCCESS("Production preflight passed."))
