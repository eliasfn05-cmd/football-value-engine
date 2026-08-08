from __future__ import annotations

import json
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from engine.features import FeatureEngineeringService
from engine.models import Fixture


class Command(BaseCommand):
    help = "Build V8 feature vectors from persisted PostgreSQL data without external API calls."

    def add_arguments(self, parser):
        parser.add_argument("--fixture-id", dest="fixture_id")
        parser.add_argument("--date", dest="target_date", help="YYYY-MM-DD")

    def handle(self, *args, **options):
        fixture_id = options.get("fixture_id")
        raw_date = options.get("target_date")
        if not fixture_id and not raw_date:
            raise CommandError("Provide --fixture-id or --date")
        if fixture_id and raw_date:
            raise CommandError("Use only one of --fixture-id or --date")

        service = FeatureEngineeringService()
        if fixture_id:
            fixture = (
                Fixture.objects.select_related("home_team", "away_team", "competition_ref")
                .filter(external_id=str(fixture_id))
                .first()
            )
            if not fixture:
                raise CommandError(f"Fixture {fixture_id} not found")
            payload = service.build(fixture).to_dict()
            self.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
            return

        try:
            target_date = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise CommandError("--date must use YYYY-MM-DD") from exc

        start = timezone.make_aware(timezone.datetime.combine(target_date, timezone.datetime.min.time()))
        end = start + timezone.timedelta(days=1)
        fixtures = (
            Fixture.objects.select_related("home_team", "away_team", "competition_ref")
            .filter(kickoff__gte=start, kickoff__lt=end)
            .order_by("kickoff")
        )
        payload = [service.build(fixture).to_dict() for fixture in fixtures]
        self.stdout.write(json.dumps({"date": raw_date, "count": len(payload), "features": payload}, indent=2, ensure_ascii=False, default=str))
