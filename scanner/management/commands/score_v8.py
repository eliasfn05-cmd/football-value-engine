from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from engine.models import Fixture
from engine.score_v8 import ScoreEngineV8, V8_MODEL_VERSION


class Command(BaseCommand):
    help = "Evaluate and persist V8 BTTS/Over 2.5 predictions from PostgreSQL features."

    def add_arguments(self, parser):
        parser.add_argument("--fixture-id", dest="fixture_id")
        parser.add_argument("--date", dest="target_date", help="YYYY-MM-DD")
        parser.add_argument("--premium-only", action="store_true")

    def handle(self, *args, **options):
        fixture_id = options.get("fixture_id")
        raw_date = options.get("target_date")
        premium_only = bool(options.get("premium_only"))
        if not fixture_id and not raw_date:
            raise CommandError("Provide --fixture-id or --date")
        if fixture_id and raw_date:
            raise CommandError("Use only one of --fixture-id or --date")

        engine = ScoreEngineV8()

        if fixture_id:
            fixture = (
                Fixture.objects.select_related("home_team", "away_team", "competition_ref")
                .filter(external_id=str(fixture_id))
                .first()
            )
            if not fixture:
                raise CommandError(f"Fixture {fixture_id} not found")
            result = engine.evaluate_and_persist(fixture)
            payload = self._fixture_payload(fixture, result, premium_only)
            self.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
            return

        try:
            target_date = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise CommandError("--date must use YYYY-MM-DD") from exc

        start = timezone.make_aware(datetime.combine(target_date, time.min))
        end = start + timedelta(days=1)
        fixtures = (
            Fixture.objects.select_related("home_team", "away_team", "competition_ref")
            .filter(kickoff__gte=start, kickoff__lt=end)
            .order_by("kickoff")
        )

        rows = []
        for fixture in fixtures:
            result = engine.evaluate_and_persist(fixture)
            row = self._fixture_payload(fixture, result, premium_only)
            if row is not None:
                rows.append(row)

        premium = []
        for row in rows:
            for market in row["markets"]:
                if market["tier"] == "TIER_A":
                    premium.append({
                        "fixture_id": row["fixture_id"],
                        "match": row["match"],
                        **market,
                    })
        premium.sort(key=lambda item: ((item.get("expected_value") or -999), item.get("score") or 0), reverse=True)

        payload = {
            "date": raw_date,
            "model_version": V8_MODEL_VERSION,
            "fixtures_scored": len(rows),
            "premium_count": len(premium),
            "premium": premium,
            "fixtures": rows if not premium_only else None,
        }
        self.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False, default=str))

    @staticmethod
    def _fixture_payload(fixture, result, premium_only):
        markets = []
        for evaluation in result.values():
            if premium_only and evaluation["tier"] != "TIER_A":
                continue
            markets.append({
                "market": evaluation["market"],
                "selection": evaluation["selection"],
                "probability": evaluation["probability"],
                "market_odds": evaluation["market_odds"],
                "fair_odds": evaluation["fair_odds"],
                "edge": evaluation["edge"],
                "expected_value": evaluation["expected_value"],
                "score": evaluation["score"],
                "tier": evaluation["tier"],
                "gate_failures": evaluation["reasons"].get("v8_gate_failures", []),
            })
        if premium_only and not markets:
            return None
        return {
            "fixture_id": fixture.external_id,
            "match": f"{fixture.home_team.name} vs {fixture.away_team.name}",
            "kickoff": fixture.kickoff.isoformat(),
            "markets": markets,
        }
