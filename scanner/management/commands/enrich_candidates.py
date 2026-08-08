from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from engine.models import Fixture, OddsSnapshot, Prediction, StandingSnapshot
from engine.score_v8 import V8_MODEL_VERSION
from scanner.ingestion import DataIngestionService
from scanner.odds import parse_quotes
from scanner.providers.api_football import APIFootballProvider


MIN_VENUE_SAMPLE = 3
HISTORY_FETCH_LAST = 20
STANDINGS_MAX_AGE_HOURS = 6


class Command(BaseCommand):
    help = (
        "Enrich only the strongest future V8 candidates with historical venue samples, "
        "live odds/lineups and competition standings. Designed as the selective fast "
        "path after the bulk daily score, never as a full-card enrichment."
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
        history_saved = 0
        history_api_calls = 0
        errors = 0
        preferred_coverage = 0
        fallback_coverage = 0
        no_odds_coverage = 0

        self.stdout.write(
            f"[enrich] future_candidates={len(fixtures)} min_score={min_score:.1f} limit={limit}"
        )

        # Quality-first backfill. One API call per team is enough to improve both
        # venue samples because the returned FT history contains home and away games.
        teams_to_backfill = self._teams_missing_venue_history(fixtures)
        self.stdout.write(
            f"[enrich] history gaps teams={len(teams_to_backfill)} min_venue_sample={MIN_VENUE_SAMPLE}"
        )
        for team, before_kickoff, missing_venues in teams_to_backfill:
            try:
                history_api_calls += 1
                raw_history = provider.team_recent_fixtures(team.external_id, last=HISTORY_FETCH_LAST)
                stored_for_team = 0
                for raw in raw_history:
                    fixture_meta = raw.get("fixture") or {}
                    raw_date = fixture_meta.get("date")
                    if not raw_date:
                        continue
                    try:
                        raw_kickoff = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
                        if timezone.is_naive(raw_kickoff):
                            raw_kickoff = timezone.make_aware(raw_kickoff)
                    except (TypeError, ValueError):
                        continue
                    if raw_kickoff >= before_kickoff:
                        continue
                    ingestion.upsert_fixture(raw)
                    stored_for_team += 1
                history_saved += stored_for_team
                self.stdout.write(
                    f"[enrich] history team={team.name} missing={','.join(sorted(missing_venues))} "
                    f"api={len(raw_history)} stored={stored_for_team}"
                )
            except Exception as exc:
                errors += 1
                self.stderr.write(f"[enrich] history error team={team.external_id}: {exc}")

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
                    recent_cutoff = timezone.now() - timedelta(hours=STANDINGS_MAX_AGE_HOURS)
                    has_fresh = StandingSnapshot.objects.filter(
                        competition=competition,
                        captured_at__gte=recent_cutoff,
                    ).exists()
                    if not has_fresh:
                        standings_saved += ingestion.ingest_standings(competition)
                except Exception as exc:
                    errors += 1
                    self.stderr.write(
                        f"[enrich] standings error competition={competition.external_id}: {exc}"
                    )

        self.stdout.write(
            f"[enrich] history coverage api_calls={history_api_calls} stored={history_saved}"
        )
        self.stdout.write(
            f"[enrich] odds coverage preferred={preferred_coverage} fallback={fallback_coverage} none={no_odds_coverage}"
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"[enrich] complete future_candidates={len(fixtures)} history={history_saved} odds={odds_saved} "
                f"lineups={lineups_saved} standings={standings_saved} errors={errors}"
            )
        )

    @staticmethod
    def _teams_missing_venue_history(fixtures: list[Fixture]):
        """Return each deficient team once, using its earliest shortlisted kickoff.

        The gate requires >=3 venue-specific historical matches. Fetching recent
        FT history once per deficient team can fill both home and away samples.
        """
        team_needs: dict[int, dict] = {}
        for fixture in fixtures:
            for team, venue in ((fixture.home_team, "home"), (fixture.away_team, "away")):
                record = team_needs.setdefault(
                    team.id,
                    {"team": team, "before": fixture.kickoff, "venues": set()},
                )
                record["before"] = min(record["before"], fixture.kickoff)
                record["venues"].add(venue)

        result = []
        for record in team_needs.values():
            team = record["team"]
            before = record["before"]
            missing: set[str] = set()
            for venue in record["venues"]:
                qs = Fixture.objects.filter(
                    kickoff__lt=before,
                    home_goals__isnull=False,
                    away_goals__isnull=False,
                )
                qs = qs.filter(home_team=team) if venue == "home" else qs.filter(away_team=team)
                if qs.count() < MIN_VENUE_SAMPLE:
                    missing.add(venue)
            if missing:
                result.append((team, before, missing))
        return result

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
