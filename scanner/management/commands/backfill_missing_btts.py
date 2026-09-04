from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from engine.competition_quality import classify_competition
from engine.models import Fixture, FixtureScoreState, Prediction
from engine.score_v8 import V8_MODEL_VERSION


class Command(BaseCommand):
    help = (
        "Self-heal missing V8 BTTS predictions for a date. If a fixture has a stale "
        "FixtureScoreState fingerprint but no BTTS Prediction, remove only that stale "
        "score state and rerun incremental score_v8 so the prediction is rebuilt."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            dest="target_date",
            help="YYYY-MM-DD. Defaults to today in America/Lima.",
        )
        parser.add_argument(
            "--no-score",
            action="store_true",
            help="Only clear stale score states; do not invoke score_v8 afterwards.",
        )

    @staticmethod
    def _bounds(target_date: date):
        start = timezone.make_aware(datetime.combine(target_date, time.min))
        return start, start + timedelta(days=1)

    def handle(self, *args, **options):
        raw_date = options.get("target_date")
        try:
            target_date = (
                date.fromisoformat(raw_date)
                if raw_date
                else datetime.now(ZoneInfo("America/Lima")).date()
            )
        except ValueError as exc:
            raise CommandError("--date must use YYYY-MM-DD") from exc

        start, end = self._bounds(target_date)
        fixtures = list(
            Fixture.objects.select_related("competition_ref")
            .filter(kickoff__gte=start, kickoff__lt=end)
            .order_by("kickoff")
        )
        eligible_ids = [f.id for f in fixtures if not classify_competition(f).excluded]
        existing_btts_ids = set(
            Prediction.objects.filter(
                model_version=V8_MODEL_VERSION,
                market__iexact="BTTS",
                fixture_id__in=eligible_ids,
            ).values_list("fixture_id", flat=True)
        )
        missing_ids = [fid for fid in eligible_ids if fid not in existing_btts_ids]

        stale_states = FixtureScoreState.objects.filter(
            model_version=V8_MODEL_VERSION,
            fixture_id__in=missing_ids,
        )
        stale_count = stale_states.count()
        if stale_count:
            stale_states.delete()

        self.stdout.write(
            f"BTTS BACKFILL PREP | date={target_date} eligible={len(eligible_ids)} "
            f"existing_btts={len(existing_btts_ids)} missing_btts={len(missing_ids)} "
            f"stale_states_cleared={stale_count}"
        )

        if not options.get("no_score") and missing_ids:
            # Incremental score_v8 will now re-evaluate every missing-BTTS fixture:
            # fixtures without state were already eligible for scoring; fixtures whose
            # stale state incorrectly caused a skip were repaired above.
            call_command(
                "score_v8",
                target_date=target_date.isoformat(),
                summary_only=True,
            )

        after_ids = set(
            Prediction.objects.filter(
                model_version=V8_MODEL_VERSION,
                market__iexact="BTTS",
                fixture_id__in=eligible_ids,
            ).values_list("fixture_id", flat=True)
        )
        still_missing = [fid for fid in eligible_ids if fid not in after_ids]
        created = max(0, len(after_ids) - len(existing_btts_ids))

        self.stdout.write(
            self.style.SUCCESS(
                f"BTTS BACKFILL RESULT | date={target_date} created={created} "
                f"btts_total={len(after_ids)}/{len(eligible_ids)} still_missing={len(still_missing)}"
            )
        )
        if still_missing:
            self.stdout.write(
                self.style.WARNING(
                    "Still missing fixture DB ids (first 25): "
                    + ",".join(str(fid) for fid in still_missing[:25])
                )
            )
