from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError

from scanner.pipeline import DailyPipeline


class Command(BaseCommand):
    help = "Run the production daily pipeline with retries and persisted observability."

    def add_arguments(self, parser):
        parser.add_argument("--date", dest="target_date", help="YYYY-MM-DD. Defaults to today in America/Lima.")
        parser.add_argument("--attempts", type=int, default=3, help="Maximum attempts per stage (default 3).")
        parser.add_argument("--retry-delay", type=float, default=1.0, help="Seconds between retries (default 1).")

    def handle(self, *args, **options):
        raw_date = options.get("target_date")
        try:
            target_date = date.fromisoformat(raw_date) if raw_date else datetime.now(ZoneInfo("America/Lima")).date()
        except ValueError as exc:
            raise CommandError("--date must use YYYY-MM-DD") from exc

        pipeline = DailyPipeline(
            max_attempts=options["attempts"],
            retry_delay_seconds=options["retry_delay"],
        ).run(target_date)

        self.stdout.write(
            f"Pipeline #{pipeline.id} {pipeline.target_date} -> {pipeline.status} | "
            f"fixtures={pipeline.fixtures_count} predictions={pipeline.predictions_count} "
            f"premium={pipeline.premium_count} settled={pipeline.settled_count} "
            f"warnings={pipeline.warning_count} errors={pipeline.error_count} "
            f"duration={pipeline.duration_seconds}s"
        )
        for stage in pipeline.stages.all():
            self.stdout.write(
                f"  {stage.name}: {stage.status} attempts={stage.attempt_count} "
                f"records={stage.records_processed} duration={stage.duration_seconds}s {stage.message}"
            )

        if pipeline.status == pipeline.STATUS_FAILED:
            raise CommandError(f"Pipeline #{pipeline.id} finished with required stage failures")

        self.stdout.write(self.style.SUCCESS(f"Pipeline #{pipeline.id} completed with status {pipeline.status}."))
