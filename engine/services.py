from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Dict, Optional

from django.db import transaction

from .model import FootballValueEngine
from .models import Fixture, OddsSnapshot, Prediction
from .quantitative import MarketEvaluation, MarketQuote, MatchContext


class PredictionService:
    """Application service that evaluates a fixture and stores immutable snapshots."""

    def __init__(self, engine: Optional[FootballValueEngine] = None):
        self.engine = engine or FootballValueEngine()

    @staticmethod
    def _latest_quote(fixture: Fixture, market: str, selection: str, bookmaker: str = "Betano") -> Optional[MarketQuote]:
        snapshot = (
            OddsSnapshot.objects.filter(
                fixture=fixture,
                market=market,
                selection=selection,
                bookmaker__iexact=bookmaker,
            )
            .order_by("-captured_at")
            .first()
        )
        if not snapshot:
            return None
        return MarketQuote(decimal_odds=float(snapshot.decimal_odds), bookmaker=snapshot.bookmaker)

    @transaction.atomic
    def evaluate_and_store(
        self,
        fixture: Fixture,
        context: MatchContext,
        bookmaker: str = "Betano",
    ) -> Dict[str, Prediction]:
        btts_quote = self._latest_quote(fixture, "BTTS", "YES", bookmaker)
        over_quote = self._latest_quote(fixture, "OVER_2_5", "OVER", bookmaker)
        evaluations = self.engine.evaluate(context, btts_quote=btts_quote, over25_quote=over_quote)

        stored: Dict[str, Prediction] = {}
        for key, evaluation in evaluations.items():
            stored[key] = self._store_prediction(fixture, evaluation)
        return stored

    @staticmethod
    def _decimal(value):
        if value is None:
            return None
        return Decimal(str(value))

    def _store_prediction(self, fixture: Fixture, evaluation: MarketEvaluation) -> Prediction:
        return Prediction.objects.create(
            fixture=fixture,
            model_version=str(evaluation.reasons.get("model_version", "unknown")),
            market=evaluation.market,
            selection=evaluation.selection,
            probability=self._decimal(evaluation.probability),
            fair_odds=self._decimal(evaluation.fair_odds),
            market_odds=self._decimal(evaluation.market_odds),
            edge=self._decimal(evaluation.edge),
            expected_value=self._decimal(evaluation.expected_value),
            score=self._decimal(evaluation.score),
            tier=evaluation.tier,
            reasons=evaluation.reasons,
        )
