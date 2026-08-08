from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from engine.models import Fixture, OddsSnapshot, Prediction, StandingSnapshot
from engine.score_v8 import V8_MODEL_VERSION
from scanner.ingestion import DataIngestionService
from scanner.odds import parse_quotes
from scanner.providers.api_football import APIFootballProvider


class Command(BaseCommand):
    help = (
        "Enrich only the strongest future V8 candidates with live odds/lineups and "
        "competition standings. Designed as the selective fast path after the "
        "bulk daily score, never as a full-card enrichment."
    )

    def add_arguments(self, parser):
        parser.add_argument("--date", dest="target_date", required=True, help="YYYY-MM-DD")
        parser.add_argument("--limit", type=int, default=20, help="Maximum unique fixtures to enrich.")
        parser.add_argument("--min-score", type=float, default=50.0, help="Minimum V8 score to enter shortlist.")

    def handle(self, *args, **options):
        try:
            target_date = date.fromisoformat(options["target_date"])
        except ValueError as exc:
            raise CommandError("--date must use YYYY-MM-DD") from exc

        limit = max(1, min(int(options["limit"]), 50))
        min_score = float(options["min_score"])
        start = timezone.make_aware(datetime.combine(target_date, time.min))
        end = start + timedelta(days=1)
        now = timezone.now()
        future_start = max(start, now)

        prediction_qs = (
            Prediction.objects.filter(
                model_version=V8_MODEL_VERSION,
                fixture__kickoff__gte=future_start,
                fixture__kickoff__lt=end,
                score__gte=min_score,
            )
            .select_related("fixture", "fixture__competition_ref")
            .order_by("-tier", "-score", "fixture__kickoff")
        )

        fixture_ids: list[int] = []
        seen: set[int] = set()
        for prediction in prediction_qs.iterator(chunk_size=500):
            if prediction.fixture_id in seen:
                continue
            seen.add(prediction.fixture_id)
            fixture_ids.append(prediction.fixture_id)
            if len(fixture_ids) >= limit:
                break

        fixtures = list(
            Fixture.objects.filter(id__in=fixture_ids)
            .select_related("home_team", "away_team", "competition_ref")
            .order_by("kickoff")
        )
        if not fixtures:
            self.stdout.write("[enrich] no future candidates matched shortlist filters")
            return

        provider = APIFootballProvider()
        ingestion = DataIngestionService(provider, progress=lambda message: self.stdout.write(message))
        standings_seen: set[int] = set()
        odds_saved = 0
        lineups_saved = 0
        standings_saved = 0
        errors = 0
        preferred_coverage = 0
        fallback_coverage = 0
        no_odds_coverage = 0

        self.stdout.write(
            f"[enrich] future_candidates={len(fixtures)} min_score={min_score:.1f} limit={limit}"
        )

        for index, fixture in enumerate(fixtures, start=1):
            self.stdout.write(
                f"[enrich] {index}/{len(fixtures)} {fixture.home_team.name} vs {fixture.away_team.name} "
                f"kickoff={fixture.kickoff.isoformat()}"
            )
            try:
                odds_payload = provider.fixture_odds(fixture.external_id)
                strict_quotes = parse_quotes(odds_payload)
                quotes = strict_quotes
                used_fallback = False
                if strict_quotes.get("btts") is None and strict_quotes.get("over25") is None:
                    quotes = parse_quotes(odds_payload, allow_fallback=True)
                    used_fallback = any(quotes.get(key) is not None for key in ("btts", "over25"))

                if any(strict_quotes.get(key) is not None for key in ("btts", "over25")):
                    preferred_coverage += 1
                elif used_fallback:
                    fallback_coverage += 1
                else:
                    no_odds_coverage += 1

                odds_saved += self._save_quote_if_changed(fixture, "BTTS", "YES", quotes.get("btts"))
                odds_saved += self._save_quote_if_changed(fixture, "OVER_2_5", "OVER", quotes.get("over25"))
            except Exception as exc:
                errors += 1
                no_odds_coverage += 1
                self.stderr.write(f"[enrich] odds error fixture={fixture.external_id}: {exc}")

            try:
                lineups_saved += ingestion.ingest_lineups(fixture)
            except Exception as exc:
                errors += 1
                self.stderr.write(f"[enrich] lineup error fixture={fixture.external_id}: {exc}")

            competition = fixture.competition_ref
            if competition and competition.id not in standings_seen:
                standings_seen.add(competition.id)
                try:
                    if not StandingSnapshot.objects.filter(competition=competition).exists():
                        standings_saved += ingestion.ingest_standings(competition)
                except Exception as exc:
                    errors += 1
                    self.stderr.write(
                        f"[enrich] standings error competition={competition.external_id}: {exc}"
                    )

        self.stdout.write(
            f"[enrich] odds coverage preferred={preferred_coverage} fallback={fallback_coverage} none={no_odds_coverage}"
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"[enrich] complete future_candidates={len(fixtures)} odds={odds_saved} "
                f"lineups={lineups_saved} standings={standings_saved} errors={errors}"
            )
        )

    @staticmethod
    def _save_quote_if_changed(fixture: Fixture, market: str, selection: str, quote) -> int:
        if quote is None:
            return 0
        value = Decimal(str(quote.decimal_odds))
        latest = (
            OddsSnapshot.objects.filter(fixture=fixture, market=market, selection=selection)
            .order_by("-captured_at")
            .first()
        )
        if latest and latest.bookmaker == quote.bookmaker and latest.decimal_odds == value:
            return 0
        OddsSnapshot.objects.create(
            fixture=fixture,
            bookmaker=quote.bookmaker,
            market=market,
            selection=selection,
            decimal_odds=value,
        )
        return 1
