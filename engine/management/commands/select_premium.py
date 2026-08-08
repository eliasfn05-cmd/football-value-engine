from datetime import date

from django.core.management.base import BaseCommand, CommandError

from engine.premium_selection import DailyPremiumSelector


class Command(BaseCommand):
    help = "Rank and persist up to three operational Premium picks for a date."

    def add_arguments(self, parser):
        parser.add_argument("--date", dest="target_date", required=True, help="YYYY-MM-DD")
        parser.add_argument("--max-picks", type=int, default=3)

    def handle(self, *args, **options):
        try:
            target_date = date.fromisoformat(options["target_date"])
        except ValueError as exc:
            raise CommandError("--date must use YYYY-MM-DD") from exc

        rows = DailyPremiumSelector(max_picks=options["max_picks"]).select(target_date)
        if not rows:
            self.stdout.write("[premium] NO BET: no prediction cleared Sprint 6 minimum quality")
            return

        for row in rows:
            prediction = row.prediction
            fixture = prediction.fixture
            self.stdout.write(
                f"[premium] #{row.rank} tier={row.premium_tier} rank_score={row.premium_rank_score} "
                f"{fixture.home_team.name} vs {fixture.away_team.name} "
                f"market={prediction.market} p={prediction.probability} odds={prediction.market_odds} "
                f"edge={prediction.edge} ev={prediction.expected_value} score={prediction.score}"
            )
        self.stdout.write(self.style.SUCCESS(f"[premium] selected={len(rows)}"))
