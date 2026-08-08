from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from engine.candidate_pool import CandidatePoolRule, high_recall_candidate_pool
from engine.models import Fixture, OddsSnapshot, Prediction, StandingSnapshot
from engine.score_v8 import V8_MODEL_VERSION
from scanner.ingestion import DataIngestionService
from scanner.odds import parse_quotes
from scanner.providers.api_football import APIFootballProvider


MIN_VENUE_SAMPLE = 3
HISTORY_FETCH_LAST = 20
HISTORY_WORKERS = 5
STANDINGS_MAX_AGE_HOURS = 6
INTERACTIVE_LIMIT = 12
INTERACTIVE_MIN_SCORE = 82.0
INTERACTIVE_MIN_EDGE = 0.07
INTERACTIVE_MIN_EV = 0.10
INTERACTIVE_LINEUP_WINDOW_HOURS = 2


class Command(BaseCommand):
    help = (
        "Enrich only the strongest future V8 candidates with historical venue samples, "
        "live odds/lineups and competition standings. Interactive mode uses the Sprint "
        "6.5 High Recall Candidate Pool instead of a score-only Top-N shortlist."
    )

    def add_arguments(self, parser):
        parser.add_argument("--date", dest="target_date", required=True, help="YYYY-MM-DD")
        parser.add_argument("--limit", type=int, default=20, help="Maximum unique fixtures to enrich.")
        parser.add_argument("--min-score", type=float, default=50.0, help="Minimum V8 score to enter legacy shortlist.")

    @staticmethod
    def _interactive_fast_enabled() -> bool:
        return os.getenv("PREMIUM_INTERACTIVE_FAST", "").strip().lower() in {"1", "true", "yes", "on"}

    def _interactive_fixture_ids(self, target_date: date, limit: int) -> tuple[list[int], dict[int, tuple[str, ...]]]:
        entries = high_recall_candidate_pool(
            target_date,
            rule=CandidatePoolRule(
                min_score=INTERACTIVE_MIN_SCORE,
                min_edge=INTERACTIVE_MIN_EDGE,
                min_ev=INTERACTIVE_MIN_EV,
                limit=limit,
            ),
        )
        return [entry.fixture_id for entry in entries], {
            entry.fixture_id: entry.entry_reasons for entry in entries
        }

    def handle(self, *args, **options):
        try:
            target_date = date.fromisoformat(options["target_date"])
        except ValueError as exc:
            raise CommandError("--date must use YYYY-MM-DD") from exc

        interactive_fast = self._interactive_fast_enabled()
        requested_limit = max(1, min(int(options["limit"]), 50))
        requested_min_score = float(options["min_score"])
        limit = min(requested_limit, INTERACTIVE_LIMIT) if interactive_fast else requested_limit
        min_score = requested_min_score
        start = timezone.make_aware(datetime.combine(target_date, time.min))
        end = start + timedelta(days=1)
        now = timezone.now()
        future_start = max(start, now)

        entry_reasons_by_fixture: dict[int, tuple[str, ...]] = {}
        if interactive_fast:
            fixture_ids, entry_reasons_by_fixture = self._interactive_fixture_ids(target_date, limit)
        else:
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
            fixture_ids = []
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
        )
        fixture_order = {fixture_id: index for index, fixture_id in enumerate(fixture_ids)}
        fixtures.sort(key=lambda fixture: fixture_order.get(fixture.id, 9999))
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

        if interactive_fast:
            self.stdout.write(
                f"[enrich] high_recall_pool={len(fixtures)} limit={limit} "
                f"rules=score>={INTERACTIVE_MIN_SCORE:.0f}|edge>={INTERACTIVE_MIN_EDGE:.2f}|ev>={INTERACTIVE_MIN_EV:.2f}"
            )
            for index, fixture in enumerate(fixtures, start=1):
                reasons = ",".join(entry_reasons_by_fixture.get(fixture.id, ())) or "unknown"
                self.stdout.write(
                    f"[enrich] pool #{index} {fixture.home_team.name} vs {fixture.away_team.name} via={reasons}"
                )
        else:
            self.stdout.write(
                f"[enrich] future_candidates={len(fixtures)} min_score={min_score:.1f} limit={limit} interactive_fast=0"
            )

        # Interactive dashboard generation must be responsive. The scheduled
        # pipeline is responsible for filling historical venue gaps. If history
        # is still insufficient, V8's data-quality gates reject the candidate
        # rather than making the user wait for dozens of historical API calls.
        teams_to_backfill = [] if interactive_fast else self._teams_missing_venue_history(fixtures)
        self.stdout.write(
            f"[enrich] history gaps teams={len(teams_to_backfill)} min_venue_sample={MIN_VENUE_SAMPLE} "
            f"workers={min(HISTORY_WORKERS, max(1, len(teams_to_backfill)))} "
            f"skipped_interactive={int(interactive_fast)}"
        )

        if teams_to_backfill:
            raw_history_by_external_id: dict[str, dict] = {}
            workers = min(HISTORY_WORKERS, len(teams_to_backfill))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(
                        self._fetch_team_history,
                        team.external_id,
                        before_kickoff,
                    ): (team, missing_venues)
                    for team, before_kickoff, missing_venues in teams_to_backfill
                }
                history_api_calls = len(futures)
                completed = 0
                for future in as_completed(futures):
                    team, missing_venues = futures[future]
                    completed += 1
                    try:
                        raw_history = future.result()
                        accepted = 0
                        for raw in raw_history:
                            external_id = str(((raw.get("fixture") or {}).get("id")) or "")
                            if not external_id:
                                continue
                            raw_history_by_external_id[external_id] = raw
                            accepted += 1
                        self.stdout.write(
                            f"[enrich] history fetched {completed}/{len(futures)} team={team.name} "
                            f"missing={','.join(sorted(missing_venues))} api={len(raw_history)} accepted={accepted}"
                        )
                    except Exception as exc:
                        errors += 1
                        self.stderr.write(f"[enrich] history error team={team.external_id}: {exc}")

            if raw_history_by_external_id:
                _fixtures, delta = ingestion._bulk_ingest_fixtures(list(raw_history_by_external_id.values()))
                history_saved = int(delta.get("created", 0)) + int(delta.get("changed", 0))
                self.stdout.write(
                    f"[enrich] history bulk unique={len(raw_history_by_external_id)} "
                    f"created={delta.get('created', 0)} changed={delta.get('changed', 0)} "
                    f"unchanged={delta.get('unchanged', 0)}"
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

            # Official lineups are usually unavailable many hours before kickoff.
            # Avoid a low-value API call in interactive mode unless kickoff is near.
            should_fetch_lineup = (
                not interactive_fast
                or fixture.kickoff <= now + timedelta(hours=INTERACTIVE_LINEUP_WINDOW_HOURS)
            )
            if should_fetch_lineup:
                try:
                    lineups_saved += ingestion.ingest_lineups(fixture)
                except Exception as exc:
                    errors += 1
                    self.stderr.write(f"[enrich] lineup error fixture={fixture.external_id}: {exc}")
            else:
                self.stdout.write(f"[enrich] lineup skipped fixture={fixture.external_id} kickoff_not_near")

            competition = fixture.competition_ref
            if not interactive_fast and competition and competition.id not in standings_seen:
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
    def _fetch_team_history(team_external_id: str, before_kickoff: datetime) -> list[dict]:
        """Fetch one team's recent FT history using an isolated HTTP session."""
        provider = APIFootballProvider()
        raw_history = provider.team_recent_fixtures(team_external_id, last=HISTORY_FETCH_LAST)
        accepted: list[dict] = []
        for raw in raw_history:
            raw_date = ((raw.get("fixture") or {}).get("date"))
            if not raw_date:
                continue
            try:
                raw_kickoff = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
                if timezone.is_naive(raw_kickoff):
                    raw_kickoff = timezone.make_aware(raw_kickoff)
            except (TypeError, ValueError):
                continue
            if raw_kickoff < before_kickoff:
                accepted.append(raw)
        return accepted

    @staticmethod
    def _teams_missing_venue_history(fixtures: list[Fixture]):
        """Return each deficient team once, using its earliest shortlisted kickoff."""
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
