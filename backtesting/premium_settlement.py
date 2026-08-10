from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from engine.models import Prediction, PremiumPublicationLedger
from .models import PredictionOutcome


def _result_for(prediction, home_goals, away_goals):
    market = prediction.market.upper()
    selection = prediction.selection.upper()
    if market == "BTTS" and selection in {"YES", "SI", "SÍ"}:
        return (PredictionOutcome.RESULT_WIN if home_goals > 0 and away_goals > 0 else PredictionOutcome.RESULT_LOSS, "btts_yes")
    if market in {"OVER_2_5", "OVER 2.5", "OVER25"} and selection in {"OVER", "OVER_2_5", "OVER 2.5"}:
        return (PredictionOutcome.RESULT_WIN if home_goals + away_goals >= 3 else PredictionOutcome.RESULT_LOSS, "over_2_5")
    return PredictionOutcome.RESULT_VOID, "unsupported_market"


def settle_published_premium(*, model_version=None):
    official_ids = PremiumPublicationLedger.objects.values_list("prediction_id", flat=True)
    qs = (
        Prediction.objects.select_related("fixture")
        .filter(
            id__in=official_ids,
            fixture__home_goals__isnull=False,
            fixture__away_goals__isnull=False,
        )
        .filter(Q(outcome__isnull=True) | Q(outcome__result=PredictionOutcome.RESULT_PENDING))
    )
    if model_version:
        qs = qs.filter(model_version=model_version)

    stake = Decimal("1")
    wins = losses = voids = settled = 0
    for prediction in qs.iterator(chunk_size=500):
        fixture = prediction.fixture
        result, reason = _result_for(prediction, fixture.home_goals, fixture.away_goals)
        if result == PredictionOutcome.RESULT_WIN:
            profit = (Decimal(prediction.market_odds or 1) - Decimal("1")) * stake
            wins += 1
        elif result == PredictionOutcome.RESULT_LOSS:
            profit = -stake
            losses += 1
        else:
            profit = Decimal("0")
            voids += 1
        PredictionOutcome.objects.update_or_create(
            prediction=prediction,
            defaults={
                "result": result,
                "home_goals": fixture.home_goals,
                "away_goals": fixture.away_goals,
                "stake_units": stake,
                "profit_units": profit,
                "settled_at": timezone.now(),
                "settlement_reason": reason,
            },
        )
        settled += 1
    return {"settled": settled, "wins": wins, "losses": losses, "voids": voids}
