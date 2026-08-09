from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from engine.candidate_pool import CandidatePoolRule, high_recall_candidate_pool
from engine.deep_analysis import DeepMatchAnalysisService
from engine.models import Fixture
from scanner.ingestion import DataIngestionService
from scanner.providers.api_football import APIFootballProvider


DEEP_FIXTURE_LIMIT = 6
DEEP_HISTORY_FETCH_LAST = 30
DEEP_HISTORY_TARGET = 10
DEEP_HISTORY_WORKERS = 6


class Command(BaseCommand):
    help = "Sprint 7.0: hydrate and deeply validate the strongest future fixtures using venue-specific last-10 history."

    def add_arguments(self, parser):
        parser.add_argument("--date", dest="target_date", required=True, help="YYYY-MM-DD")
        parser.add_argument("--limit", type=int, default=DEEP_FIXTURE_LIMIT)

    def handle(self, *args, **options):
        try:
            target_date = date.fromisoformat(options["target_date"])
        except ValueError as exc:
            raise CommandError("--date must use YYYY-MM-DD") from exc

        limit = max(1, min(int(options["limit"]), DEEP_FIXTURE_LIMIT))
        pool = high_recall_candidate_pool(
            target_date,
            rule=CandidatePoolRule(limit=limit),
        )
        fixture_ids = [entry.fixture_id for entry in pool]
        fixtures = list(
            Fixture.objects.filter(id__in=fixture_ids)
            .select_related("home_team", "away_team", "competition_ref")
        )
        order = {fixture_id: index for index, fixture_id in enumerate(fixture_ids)}
        fixtures.sort(key=lambda item: order.get(item.id, 999))
        if not fixtures:
            self.stdout.write("[deep] no candidates")
            return

        needs: dict[int, tuple[object, datetime, set[str]]] = {}
        for fixture in fixtures:
            if fixture.kickoff <= timezone.now():
                continue
            for team, venue in ((fixture.home_team, "home"), (fixture.away_team, "away")):
                count = self._venue_history_count(team, venue, fixture)
                if count >= DEEP_HISTORY_TARGET:
                    continue
                previous = needs.get(team.id)
                if previous is None:
                    needs[team.id] = (team, fixture.kickoff, {venue})
                else:
                    old_team, old_before, venues = previous
                    venues.add(venue)
                    needs[team.id] = (old_team, min(old_before, fixture.kickoff), venues)

        self.stdout.write(
            f"[deep] fixtures={len(fixtures)} teams_needing_history={len(needs)} target={DEEP_HISTORY_TARGET}"
        )
        provider = APIFootballProvider()
        ingestion = DataIngestionService(provider, progress=lambda message: self.stdout.write(message))
        raw_by_external_id: dict[str, dict] = {}
        errors = 0

        if needs:
            with ThreadPoolExecutor(max_workers=min(DEEP_HISTORY_WORKERS, len(needs))) as executor:
                futures = {
                    executor.submit(self._fetch_history, team.external_id, before): (team, venues)
                    for team, before, venues in needs.values()
                }
                for future in as_completed(futures):
                    team, venues = futures[future]
                    try:
                        rows = future.result()
                        for row in rows:
                            external_id = str(((row.get("fixture") or {}).get("id")) or "")
                            if external_id:
                                raw_by_external_id[external_id] = row
                        self.stdout.write(
                            f"[deep] history team={team.name} venues={','.join(sorted(venues))} accepted={len(rows)}"
                        )
                    except Exception as exc:
                        errors += 1
                        self.stderr.write(f"[deep] history error team={team.external_id}: {exc}")

        if raw_by_external_id:
            _rows, delta = ingestion._bulk_ingest_fixtures(list(raw_by_external_id.values()))
            self.stdout.write(
                f"[deep] history persisted unique={len(raw_by_external_id)} created={delta.get('created', 0)} "
                f"changed={delta.get('changed', 0)} unchanged={delta.get('unchanged', 0)}"
            )

        service = DeepMatchAnalysisService(sample_size=DEEP_HISTORY_TARGET)
        analyzed = 0
        preferred = 0
        for fixture in fixtures:
            if fixture.kickoff <= timezone.now():
                continue
            rows = service.analyze_fixture(fixture)
            analyzed += len(rows)
            preferred += sum(1 for row in rows if (row.reasons or {}).get("deep_preferred_market"))
            if rows:
                home_evidence = (rows[0].reasons or {}).get("deep_analysis_evidence") or {}
                self.stdout.write(
                    f"[deep] {fixture.home_team.name} vs {fixture.away_team.name} "
                    f"home_n={home_evidence.get('home_sample')} away_n={home_evidence.get('away_sample')} "
                    f"home_over={home_evidence.get('home_over25_rate')} away_over={home_evidence.get('away_over25_rate')}"
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"[deep] complete fixtures={len(fixtures)} predictions={analyzed} preferred_markets={preferred} errors={errors}"
            )
        )

    @staticmethod
    def _venue_history_count(team, venue: str, fixture: Fixture) -> int:
        qs = Fixture.objects.filter(
            kickoff__lt=fixture.kickoff,
            home_goals__isnull=False,
            away_goals__isnull=False,
        )
        qs = qs.filter(home_team=team) if venue == "home" else qs.filter(away_team=team)
        return qs.count()

    @staticmethod
    def _fetch_history(team_external_id: str, before_kickoff: datetime) -> list[dict]:
        provider = APIFootballProvider()
        rows = provider.team_recent_fixtures(team_external_id, last=DEEP_HISTORY_FETCH_LAST)
        accepted: list[dict] = []
        for row in rows:
            raw_date = ((row.get("fixture") or {}).get("date"))
            if not raw_date:
                continue
            try:
                raw_kickoff = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
                if timezone.is_naive(raw_kickoff):
                    raw_kickoff = timezone.make_aware(raw_kickoff)
            except (TypeError, ValueError):
                continue
            if raw_kickoff < before_kickoff:
                accepted.append(row)
        return accepted
