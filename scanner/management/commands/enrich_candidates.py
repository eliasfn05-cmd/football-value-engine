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
from scanner.providers.odds_api_io import OddsApiIoProvider

MIN_VENUE_SAMPLE = 3
HISTORY_FETCH_LAST = 20
HISTORY_WORKERS = 5
STANDINGS_MAX_AGE_HOURS = 6
INTERACTIVE_LIMIT = 40
INTERACTIVE_MIN_SCORE = 78.0
INTERACTIVE_MIN_EDGE = 0.05
INTERACTIVE_MIN_EV = 0.06
INTERACTIVE_LINEUP_WINDOW_HOURS = 2

class Command(BaseCommand):
    help = "Enrich strongest future V8 candidates with history, multi-source odds, lineups and standings."

    def add_arguments(self, parser):
        parser.add_argument("--date", dest="target_date", required=True, help="YYYY-MM-DD")
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument("--min-score", type=float, default=50.0)

    @staticmethod
    def _interactive_fast_enabled() -> bool:
        return os.getenv("PREMIUM_INTERACTIVE_FAST", "").strip().lower() in {"1", "true", "yes", "on"}

    def _interactive_fixture_ids(self, target_date, limit):
        entries = high_recall_candidate_pool(target_date, rule=CandidatePoolRule(min_score=INTERACTIVE_MIN_SCORE, min_edge=INTERACTIVE_MIN_EDGE, min_ev=INTERACTIVE_MIN_EV, limit=limit))
        return [e.fixture_id for e in entries], {e.fixture_id: e.entry_reasons for e in entries}

    def handle(self, *args, **options):
        try:
            target_date = date.fromisoformat(options["target_date"])
        except ValueError as exc:
            raise CommandError("--date must use YYYY-MM-DD") from exc
        interactive_fast = self._interactive_fast_enabled()
        requested_limit = max(1, min(int(options["limit"]), 50))
        limit = min(requested_limit, INTERACTIVE_LIMIT) if interactive_fast else requested_limit
        start = timezone.make_aware(datetime.combine(target_date, time.min)); end = start + timedelta(days=1); now = timezone.now(); future_start = max(start, now)
        entry_reasons_by_fixture = {}
        if interactive_fast:
            fixture_ids, entry_reasons_by_fixture = self._interactive_fixture_ids(target_date, limit)
        else:
            qs = Prediction.objects.filter(model_version=V8_MODEL_VERSION, fixture__kickoff__gte=future_start, fixture__kickoff__lt=end, score__gte=float(options["min_score"])).select_related("fixture").order_by("-tier", "-score", "fixture__kickoff")
            fixture_ids=[]; seen=set()
            for p in qs.iterator(chunk_size=500):
                if p.fixture_id not in seen:
                    seen.add(p.fixture_id); fixture_ids.append(p.fixture_id)
                    if len(fixture_ids)>=limit: break
        fixtures=list(Fixture.objects.filter(id__in=fixture_ids).select_related("home_team","away_team","competition_ref")); order={x:i for i,x in enumerate(fixture_ids)}; fixtures.sort(key=lambda f:order.get(f.id,9999))
        if not fixtures:
            self.stdout.write("[enrich] no future candidates matched shortlist filters"); return
        provider=APIFootballProvider(); secondary=OddsApiIoProvider(); ingestion=DataIngestionService(provider, progress=lambda m:self.stdout.write(m))
        odds_saved=lineups_saved=standings_saved=history_saved=errors=preferred_coverage=fallback_coverage=secondary_coverage=no_odds_coverage=0; standings_seen=set()
        teams_to_backfill=[] if interactive_fast else self._teams_missing_venue_history(fixtures)
        if teams_to_backfill:
            raw_by_id={}; workers=min(HISTORY_WORKERS,len(teams_to_backfill))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures={executor.submit(self._fetch_team_history,t.external_id,before):(t,missing) for t,before,missing in teams_to_backfill}
                for future in as_completed(futures):
                    try:
                        for raw in future.result():
                            eid=str(((raw.get("fixture") or {}).get("id")) or "")
                            if eid: raw_by_id[eid]=raw
                    except Exception as exc:
                        errors+=1; self.stderr.write(f"[enrich] history error: {exc}")
            if raw_by_id:
                _,delta=ingestion._bulk_ingest_fixtures(list(raw_by_id.values())); history_saved=int(delta.get("created",0))+int(delta.get("changed",0))
        for index, fixture in enumerate(fixtures,start=1):
            self.stdout.write(f"[enrich] {index}/{len(fixtures)} {fixture.home_team.name} vs {fixture.away_team.name}")
            try:
                primary_payload=provider.fixture_odds(fixture.external_id)
                strict=parse_quotes(primary_payload)
                quotes=parse_quotes(primary_payload, allow_fallback=True)
                if any(strict.get(k) is not None for k in ("btts","over25")): preferred_coverage+=1
                elif any(quotes.get(k) is not None for k in ("btts","over25")): fallback_coverage+=1
                missing=[k for k in ("btts","over25") if quotes.get(k) is None]
                if missing and secondary.configured:
                    fixture_row={"fixture":{"id":fixture.external_id,"date":fixture.kickoff.isoformat()},"teams":{"home":{"name":fixture.home_team.name},"away":{"name":fixture.away_team.name}}}
                    secondary_payload=secondary.fixture_odds_as_api_football(fixture_row)
                    secondary_quotes=parse_quotes(secondary_payload, allow_fallback=True)
                    filled=0
                    for key in missing:
                        if secondary_quotes.get(key) is not None:
                            quotes[key]=secondary_quotes[key]; filled+=1
                    if filled:
                        secondary_coverage+=1; self.stdout.write(f"[enrich] secondary_odds fixture={fixture.external_id} filled={filled} source=odds-api.io")
                if quotes.get("btts") is None and quotes.get("over25") is None: no_odds_coverage+=1
                odds_saved+=self._save_quote_if_changed(fixture,"BTTS","YES",quotes.get("btts")); odds_saved+=self._save_quote_if_changed(fixture,"OVER_2_5","OVER",quotes.get("over25"))
            except Exception as exc:
                errors+=1; no_odds_coverage+=1; self.stderr.write(f"[enrich] odds error fixture={fixture.external_id}: {exc}")
            if not interactive_fast or fixture.kickoff <= now+timedelta(hours=INTERACTIVE_LINEUP_WINDOW_HOURS):
                try: lineups_saved+=ingestion.ingest_lineups(fixture)
                except Exception as exc: errors+=1; self.stderr.write(f"[enrich] lineup error fixture={fixture.external_id}: {exc}")
            competition=fixture.competition_ref
            if not interactive_fast and competition and competition.id not in standings_seen:
                standings_seen.add(competition.id)
                try:
                    cutoff=timezone.now()-timedelta(hours=STANDINGS_MAX_AGE_HOURS)
                    if not StandingSnapshot.objects.filter(competition=competition,captured_at__gte=cutoff).exists(): standings_saved+=ingestion.ingest_standings(competition)
                except Exception as exc: errors+=1; self.stderr.write(f"[enrich] standings error: {exc}")
        self.stdout.write(f"[enrich] odds coverage preferred={preferred_coverage} api_football_fallback={fallback_coverage} secondary={secondary_coverage} none={no_odds_coverage} secondary_configured={int(secondary.configured)}")
        self.stdout.write(self.style.SUCCESS(f"[enrich] complete future_candidates={len(fixtures)} history={history_saved} odds={odds_saved} lineups={lineups_saved} standings={standings_saved} errors={errors}"))

    @staticmethod
    def _fetch_team_history(team_external_id, before_kickoff):
        provider=APIFootballProvider(); accepted=[]
        for raw in provider.team_recent_fixtures(team_external_id,last=HISTORY_FETCH_LAST):
            raw_date=((raw.get("fixture") or {}).get("date"))
            if not raw_date: continue
            try:
                k=datetime.fromisoformat(str(raw_date).replace("Z","+00:00")); k=timezone.make_aware(k) if timezone.is_naive(k) else k
            except (TypeError,ValueError): continue
            if k<before_kickoff: accepted.append(raw)
        return accepted

    @staticmethod
    def _teams_missing_venue_history(fixtures):
        needs={}
        for fixture in fixtures:
            for team,venue in ((fixture.home_team,"home"),(fixture.away_team,"away")):
                rec=needs.setdefault(team.id,{"team":team,"before":fixture.kickoff,"venues":set()}); rec["before"]=min(rec["before"],fixture.kickoff); rec["venues"].add(venue)
        result=[]
        for rec in needs.values():
            missing=set()
            for venue in rec["venues"]:
                qs=Fixture.objects.filter(kickoff__lt=rec["before"],home_goals__isnull=False,away_goals__isnull=False); qs=qs.filter(home_team=rec["team"]) if venue=="home" else qs.filter(away_team=rec["team"])
                if qs.count()<MIN_VENUE_SAMPLE: missing.add(venue)
            if missing: result.append((rec["team"],rec["before"],missing))
        return result

    @staticmethod
    def _save_quote_if_changed(fixture,market,selection,quote):
        if quote is None: return 0
        value=Decimal(str(quote.decimal_odds)); latest=OddsSnapshot.objects.filter(fixture=fixture,market=market,selection=selection).order_by("-captured_at").first()
        if latest and latest.bookmaker==quote.bookmaker and latest.decimal_odds==value: return 0
        OddsSnapshot.objects.create(fixture=fixture,bookmaker=quote.bookmaker,market=market,selection=selection,decimal_odds=value); return 1
