from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from engine.model import FootballValueEngine
from engine.models import Fixture, OddsSnapshot, Prediction, Team
from engine.quantitative import MODEL_VERSION, MarketEvaluation

from .context import enrich_match_context
from .odds import parse_quotes
from .profiles import build_match_context
from .providers.base import SportsDataProvider


class DailyScanner:
    def __init__(self, provider: SportsDataProvider, engine: FootballValueEngine | None = None):
        self.provider = provider
        self.engine = engine or FootballValueEngine()

    @staticmethod
    def _kickoff(raw: dict) -> datetime:
        value = ((raw.get("fixture") or {}).get("date"))
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if timezone.is_aware(dt) else timezone.make_aware(dt)

    @staticmethod
    def _status(raw: dict) -> str:
        return str((((raw.get("fixture") or {}).get("status") or {}).get("short") or "scheduled"))

    @transaction.atomic
    def _upsert_fixture(self, raw: dict) -> Fixture:
        teams = raw.get("teams") or {}
        league = raw.get("league") or {}
        fixture_meta = raw.get("fixture") or {}
        home_raw = teams.get("home") or {}
        away_raw = teams.get("away") or {}

        home, _ = Team.objects.update_or_create(
            external_id=str(home_raw.get("id")),
            defaults={"name": home_raw.get("name", "Unknown"), "country": ""},
        )
        away, _ = Team.objects.update_or_create(
            external_id=str(away_raw.get("id")),
            defaults={"name": away_raw.get("name", "Unknown"), "country": ""},
        )
        venue = (fixture_meta.get("venue") or {}).get("name", "")
        fixture, _ = Fixture.objects.update_or_create(
            external_id=str(fixture_meta.get("id")),
            defaults={
                "competition": league.get("name", "Unknown"),
                "kickoff": self._kickoff(raw),
                "home_team": home,
                "away_team": away,
                "venue": venue or "",
                "status": self._status(raw),
            },
        )
        return fixture

    @staticmethod
    def _save_quote(fixture: Fixture, market: str, selection: str, quote) -> None:
        if quote is None:
            return
        OddsSnapshot.objects.create(
            fixture=fixture,
            bookmaker=quote.bookmaker,
            market=market,
            selection=selection,
            decimal_odds=Decimal(str(quote.decimal_odds)),
        )

    @staticmethod
    def _save_prediction(fixture: Fixture, evaluation: MarketEvaluation) -> Prediction:
        return Prediction.objects.create(
            fixture=fixture,
            model_version=MODEL_VERSION,
            market=evaluation.market,
            selection=evaluation.selection,
            probability=Decimal(str(evaluation.probability)),
            fair_odds=Decimal(str(evaluation.fair_odds)),
            market_odds=Decimal(str(evaluation.market_odds)) if evaluation.market_odds is not None else None,
            edge=Decimal(str(evaluation.edge)) if evaluation.edge is not None else None,
            expected_value=Decimal(str(evaluation.expected_value)) if evaluation.expected_value is not None else None,
            score=Decimal(str(evaluation.score)),
            tier=evaluation.tier,
            reasons=evaluation.reasons,
        )

    def scan_fixture(self, raw_fixture: dict) -> dict:
        fixture = self._upsert_fixture(raw_fixture)
        teams = raw_fixture.get("teams") or {}
        home_id = (teams.get("home") or {}).get("id")
        away_id = (teams.get("away") or {}).get("id")
        external_fixture_id = (raw_fixture.get("fixture") or {}).get("id")

        home_history = self.provider.team_recent_fixtures(home_id, last=12)
        away_history = self.provider.team_recent_fixtures(away_id, last=12)
        h2h = self.provider.head_to_head(home_id, away_id, last=5)
        odds_payload = self.provider.fixture_odds(external_fixture_id)
        quotes = parse_quotes(odds_payload)

        context = build_match_context(raw_fixture, home_history, away_history, h2h)
        context, advanced_context = enrich_match_context(
            self.provider,
            raw_fixture,
            context,
            home_history,
            away_history,
        )

        evaluations = self.engine.evaluate(
            context,
            btts_quote=quotes["btts"],
            over25_quote=quotes["over25"],
        )
        for evaluation in evaluations.values():
            evaluation.reasons.update(advanced_context)

        self._save_quote(fixture, "BTTS", "YES", quotes["btts"])
        self._save_quote(fixture, "OVER_2_5", "OVER", quotes["over25"])
        saved = {name: self._save_prediction(fixture, evaluation) for name, evaluation in evaluations.items()}

        return {
            "fixture_id": fixture.external_id,
            "fixture": str(fixture),
            "kickoff": fixture.kickoff.isoformat(),
            "advanced_context": advanced_context,
            "btts": asdict(evaluations["btts"]),
            "over25": asdict(evaluations["over25"]),
            "prediction_ids": {name: prediction.id for name, prediction in saved.items()},
        }

    def scan_date(self, target_date: date) -> dict:
        fixtures = self.provider.fixtures_by_date(target_date)
        results: list[dict] = []
        errors: list[dict] = []
        for raw in fixtures:
            try:
                results.append(self.scan_fixture(raw))
            except Exception as exc:  # One bad fixture must not abort the daily board.
                fixture_id = ((raw.get("fixture") or {}).get("id"))
                errors.append({"fixture_id": fixture_id, "error": str(exc)})

        tier_a: list[dict] = []
        for result in results:
            for key in ("btts", "over25"):
                evaluation = result[key]
                if evaluation.get("tier") == "TIER_A":
                    tier_a.append({
                        "fixture_id": result["fixture_id"],
                        "fixture": result["fixture"],
                        "kickoff": result["kickoff"],
                        **evaluation,
                    })
        tier_a.sort(key=lambda row: (row.get("expected_value") or -999, row.get("score") or 0), reverse=True)
        for rank, row in enumerate(tier_a, start=1):
            row["rank"] = rank

        return {
            "date": target_date.isoformat(),
            "fixtures_scanned": len(results),
            "errors": errors,
            "tier_a": tier_a,
        }
