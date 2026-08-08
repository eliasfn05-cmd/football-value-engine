from __future__ import annotations

import os
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
        "Enrich only the strongest V8 candidates with live odds/lineups and "
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

        prediction_qs = (
            Prediction.objects.filter(
                model_version=V8_MODEL_VERSION,
                fixture__kickoff__gte=start,
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
            self.stdout.write("[enrich] no candidates matched shortlist filters")
            return

        provider = APIFootballProvider()
        ingestion = DataIngestionService(provider, progress=lambda message: self.stdout.write(message))
        standings_seen: set[int] = set()
        odds_saved = 0
        lineups_saved = 0
        standings_saved = 0
        errors = 0
        preferred_hits = 0
        fallback_hits = 0
        no_odds = 0
        preferred_name = os.getenv("PREFERRED_BOOKMAKER", "Betano").strip().lower()

        self.stdout.write(
            f"[enrich] candidates={len(fixtures)} min_score={min_score:.1f} limit={limit}"
        )

        for index, fixture in enumerate(fixtures, start=1):
            self.stdout.write(
                f"[enrich] {index}/{len(fixtures)} {fixture.home_team.name} vs {fixture.away_team.name}"
            )
            try:
                odds_payload = provider.fixture_odds(fixture.external_id)
                quotes = parse_quotes(odds_payload, allow_fallback=True)
                quote_bookmakers = {
                    quote.bookmaker.strip().lower()
                    for quote in quotes.values()
                    if quote is not None and quote.bookmaker
                }
                if not quote_bookmakers:
                    no_odds += 1
                elif preferred_name in quote_bookmakers:
                    preferred_hits += 1
                else:
                    fallback_hits += 1

                odds_saved += self._save_quote_if_changed(fixture, "BTTS", "YES", quotes.get("btts"))
                odds_saved += self._save_quote_if_changed(fixture, "OVER_2_5", "OVER", quotes.get("over25"))
            except Exception as exc:
                errors += 1
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
            f"[enrich] odds coverage preferred={preferred_hits} fallback={fallback_hits} none={no_odds}"
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"[enrich] complete candidates={len(fixtures)} odds={odds_saved} "
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
