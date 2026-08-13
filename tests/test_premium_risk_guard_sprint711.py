from types import SimpleNamespace

from engine.premium_risk_guard import PremiumRiskGuard


def _prediction(**evidence):
    return SimpleNamespace(
        market="OVER_2_5",
        reasons={"deep_analysis_evidence": evidence},
    )


def _base(**overrides):
    evidence = {
        "home_recent_n": 5,
        "away_recent_n": 5,
        "home_recent_over25_rate": 0.80,
        "away_recent_over25_rate": 0.60,
        "home_recent_failed_to_score_rate": 0.20,
        "away_recent_failed_to_score_rate": 0.20,
        "home_clean_sheet_rate": 0.20,
        "away_clean_sheet_rate": 0.20,
        "market_support_index": 0.70,
    }
    evidence.update(overrides)
    return evidence


def test_over25_blocks_two_of_five_venue_side():
    decision = PremiumRiskGuard.evaluate(
        _prediction(**_base(away_recent_over25_rate=0.40))
    )
    assert decision.blocked is True
    assert decision.code == "venue_recent_over25_hard_floor"


def test_over25_requires_at_least_one_four_of_five_anchor():
    decision = PremiumRiskGuard.evaluate(
        _prediction(**_base(home_recent_over25_rate=0.60, away_recent_over25_rate=0.60))
    )
    assert decision.blocked is True
    assert decision.code == "over25_no_strong_venue_anchor"


def test_over25_blocks_weak_combined_recent_signal():
    decision = PremiumRiskGuard.evaluate(
        _prediction(**_base(home_recent_over25_rate=0.80, away_recent_over25_rate=0.60))
    )
    assert decision.blocked is False


def test_over25_blocks_low_deep_market_support_even_with_good_rates():
    decision = PremiumRiskGuard.evaluate(
        _prediction(**_base(market_support_index=0.62))
    )
    assert decision.blocked is True
    assert decision.code == "over25_market_support_hard_floor"


def test_over25_blocks_nil_risk_home_side():
    decision = PremiumRiskGuard.evaluate(
        _prediction(**_base(
            home_recent_failed_to_score_rate=0.40,
            away_clean_sheet_rate=0.40,
        ))
    )
    assert decision.blocked is True
    assert decision.code == "over25_nil_risk_home"


def test_over25_strong_two_sided_profile_survives():
    decision = PremiumRiskGuard.evaluate(_prediction(**_base()))
    assert decision.blocked is False
