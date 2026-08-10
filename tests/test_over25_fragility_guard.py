from types import SimpleNamespace

from engine.premium_selection import DailyPremiumSelector


def _prediction(*, probability=0.579, odds=2.18, home_btts=0.50, away_btts=0.50):
    return SimpleNamespace(
        market="OVER_2_5",
        market_odds=odds,
        probability=probability,
        reasons={
            "deep_analysis_evidence": {
                "home_btts_rate": home_btts,
                "away_btts_rate": away_btts,
                "market_support_index": 0.64,
                "sample_coverage": 1.0,
                "total_deep_penalty": 0.0,
            },
            "data_quality_score": 85.0,
            "venue_sample_confidence": 1.0,
        },
    )


def test_borderline_high_price_over_with_neutral_btts_is_fragile():
    prediction = _prediction()
    assert DailyPremiumSelector._fragile_over25_profile(prediction) is True


def test_stronger_probability_is_not_blocked_by_fragility_guard():
    prediction = _prediction(probability=0.62)
    assert DailyPremiumSelector._fragile_over25_profile(prediction) is False


def test_two_sided_scoring_support_clears_fragility_guard():
    prediction = _prediction(home_btts=0.60, away_btts=0.60)
    assert DailyPremiumSelector._fragile_over25_profile(prediction) is False


def test_lower_price_over_is_not_targeted_by_guard():
    prediction = _prediction(odds=1.90)
    assert DailyPremiumSelector._fragile_over25_profile(prediction) is False
