from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand

from backtesting.models import PredictionOutcome
from engine.score_v8 import V8_MODEL_VERSION


class Command(BaseCommand):
    help = "Backtest borderline high-price Over 2.5 profiles and the Sprint 7.6 fragility guard."

    def add_arguments(self, parser):
        parser.add_argument("--model-version", default=V8_MODEL_VERSION)

    @staticmethod
    def _is_fragile(prediction) -> bool:
        if prediction.market != "OVER_2_5" or prediction.market_odds is None:
            return False
        probability = float(prediction.probability or 0.0)
        odds = float(prediction.market_odds)
        if probability < 0.56 or probability >= 0.60 or odds < 2.00 or odds > 2.40:
            return False
        evidence = (prediction.reasons or {}).get("deep_analysis_evidence") or {}
        try:
            home_btts = float(evidence.get("home_btts_rate"))
            away_btts = float(evidence.get("away_btts_rate"))
        except (TypeError, ValueError):
            return False
        return (home_btts + away_btts) / 2.0 <= 0.50

    @staticmethod
    def _summary(rows):
        rows = list(rows)
        wins = sum(row.result == PredictionOutcome.RESULT_WIN for row in rows)
        losses = sum(row.result == PredictionOutcome.RESULT_LOSS for row in rows)
        decided = wins + losses
        stake = sum((Decimal(row.stake_units) for row in rows if row.result != PredictionOutcome.RESULT_VOID), Decimal("0"))
        profit = sum((Decimal(row.profit_units) for row in rows if row.result != PredictionOutcome.RESULT_VOID), Decimal("0"))
        return {
            "n": len(rows),
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / decided) if decided else None,
            "profit": float(profit),
            "roi": float(profit / stake) if stake else None,
        }

    def handle(self, *args, **options):
        outcomes = list(
            PredictionOutcome.objects.select_related("prediction", "prediction__fixture")
            .filter(prediction__model_version=options["model_version"], prediction__market="OVER_2_5")
            .exclude(result=PredictionOutcome.RESULT_PENDING)
        )
        priced = [
            row for row in outcomes
            if row.prediction.market_odds is not None
            and 1.60 <= float(row.prediction.market_odds) <= 2.40
            and row.result != PredictionOutcome.RESULT_VOID
        ]
        fragile = [row for row in priced if self._is_fragile(row.prediction)]
        robust = [row for row in priced if not self._is_fragile(row.prediction)]

        for name, rows in (("all_priced_over25", priced), ("fragile_over25", fragile), ("non_fragile_over25", robust)):
            s = self._summary(rows)
            wr = "n/a" if s["win_rate"] is None else f"{s['win_rate'] * 100:.1f}%"
            roi = "n/a" if s["roi"] is None else f"{s['roi'] * 100:.1f}%"
            self.stdout.write(
                f"[over25-backtest] {name}: n={s['n']} W={s['wins']} L={s['losses']} "
                f"win_rate={wr} profit={s['profit']:.2f}u roi={roi}"
            )

        self.stdout.write(
            self.style.SUCCESS(
                "[over25-backtest] Sprint 7.6 guard cohort = raw p 56-60%, odds 2.00-2.40, "
                "Deep combined BTTS <= 50%."
            )
        )
