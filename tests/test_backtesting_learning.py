from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from backtesting.models import PredictionOutcome
from backtesting.services import LearningAnalyticsService, SettlementService
from engine.models import Fixture, Prediction, Team
from engine.score_v8 import V8_MODEL_VERSION


class BacktestingLearningTests(TestCase):
    def setUp(self):
        self.home = Team.objects.create(external_id="bt-home", name="Home")
        self.away = Team.objects.create(external_id="bt-away", name="Away")

    def _fixture(self, external_id, hg, ag):
        return Fixture.objects.create(
            external_id=external_id,
            competition="Test League",
            kickoff=timezone.now(),
            home_team=self.home,
            away_team=self.away,
            status="FT",
            home_goals=hg,
            away_goals=ag,
        )

    def _prediction(self, fixture, market, selection, odds, reasons=None, tier="TIER_A"):
        return Prediction.objects.create(
            fixture=fixture,
            model_version=V8_MODEL_VERSION,
            market=market,
            selection=selection,
            probability=Decimal("0.70000"),
            fair_odds=Decimal("1.429"),
            market_odds=Decimal(str(odds)),
            edge=Decimal("0.08000"),
            expected_value=Decimal("0.12000"),
            score=Decimal("88.00"),
            tier=tier,
            reasons=reasons or {},
        )

    def test_settlement_profit_is_correct_and_idempotent(self):
        fixture = self._fixture("settle-win", 2, 1)
        prediction = self._prediction(fixture, "OVER_2_5", "OVER", "1.80")
        service = SettlementService()

        first = service.settle_prediction(prediction)
        second = service.settle_prediction(prediction)

        self.assertEqual(first.result, PredictionOutcome.RESULT_WIN)
        self.assertEqual(first.profit_units, Decimal("0.8000"))
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(PredictionOutcome.objects.count(), 1)

    def test_losing_pick_costs_one_unit(self):
        fixture = self._fixture("settle-loss", 0, 0)
        prediction = self._prediction(fixture, "BTTS", "YES", "1.90")
        outcome = SettlementService().settle_prediction(prediction)
        self.assertEqual(outcome.result, PredictionOutcome.RESULT_LOSS)
        self.assertEqual(outcome.profit_units, Decimal("-1.0000"))

    def test_learning_report_attributes_rules_and_roi(self):
        win_fixture = self._fixture("learn-win", 2, 1)
        loss_fixture = self._fixture("learn-loss", 1, 0)

        win = self._prediction(
            win_fixture,
            "OVER_2_5",
            "OVER",
            "2.00",
            reasons={"early_season_penalty": 0.04, "away_over25_last5_away": 0.20},
        )
        loss = self._prediction(
            loss_fixture,
            "OVER_2_5",
            "OVER",
            "2.00",
            reasons={"early_season_penalty": 0.04, "away_over25_last5_away": 0.20},
        )
        settlement = SettlementService()
        settlement.settle_prediction(win)
        settlement.settle_prediction(loss)

        report = LearningAnalyticsService().report(model_version=V8_MODEL_VERSION, premium_only=True)
        by_scope = {item.scope: item for item in report}

        self.assertEqual(by_scope["all"].sample_size, 2)
        self.assertEqual(by_scope["all"].wins, 1)
        self.assertEqual(by_scope["all"].losses, 1)
        self.assertEqual(by_scope["all"].roi, 0.0)
        self.assertEqual(by_scope["rule:early_season"].sample_size, 2)
        self.assertEqual(by_scope["rule:ahpc_low_away_over"].sample_size, 2)
