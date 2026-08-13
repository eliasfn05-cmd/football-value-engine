from __future__ import annotations

from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from engine.batch_features import BatchFeatureEngineeringService
from engine.candidate_pool import CandidatePoolRule, high_recall_candidate_pool
from engine.models import Fixture
from engine.score_v8 import ScoreEngineV8


class Command(BaseCommand):
    help = "Batch-rescore the Premium candidate pool using one shared feature preload."

    def add_arguments(self, parser):
        parser.add_argument("--date", dest="target_date", required=True, help="YYYY-MM-DD")
        parser.add_argument("--limit", type=int, default=40)

    def handle(self, *args, **options):
        try:
            target_date = date.fromisoformat(options["target_date"])
        except ValueError as exc:
            raise CommandError("--date must use YYYY-MM-DD") from exc

        limit = max(1, min(int(options.get("limit") or 40), 60))
        pool = high_recall_candidate_pool(target_date, rule=CandidatePoolRule(limit=limit))
        ordered_ids = [entry.fixture_id for entry in pool]
        if not ordered_ids:
            self.stdout.write("[rescore_batch] no candidates to rescore")
            return

        fixtures = list(
            Fixture.objects.filter(id__in=ordered_ids, kickoff__gt=timezone.now())
            .select_related("home_team", "away_team", "competition_ref")
        )
        by_id = {fixture.id: fixture for fixture in fixtures}
        fixtures = [by_id[fid] for fid in ordered_ids if fid in by_id]
        if not fixtures:
            self.stdout.write("[rescore_batch] no future candidates to rescore")
            return

        self.stdout.write(
            f"[rescore_batch] preloading shared features for {len(fixtures)} candidate fixtures..."
        )
        preloader = BatchFeatureEngineeringService(
            fixtures,
            progress=lambda message: self.stdout.write(message),
        )
        preloader.preload()

        engine = ScoreEngineV8()
        rescored = 0
        for index, fixture in enumerate(fixtures, start=1):
            features = preloader.build(fixture)
            engine.evaluate_and_persist(fixture, features)
            rescored += 1
            if index == 1 or index % 10 == 0 or index == len(fixtures):
                self.stdout.write(f"[rescore_batch] rescored {index}/{len(fixtures)}")

        self.stdout.write(self.style.SUCCESS(f"[rescore_batch] complete: {rescored} fixtures"))
