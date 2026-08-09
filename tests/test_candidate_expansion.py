from engine.candidate_pool import (
    CandidatePoolRule,
    DISCOVERY_BTTS_PROBABILITY,
    DISCOVERY_MIN_SCORE,
    DISCOVERY_OVER25_PROBABILITY,
    _market_discovery_floor,
)
from engine.premium_selection import TIER_RULES


def test_sprint74_discovery_is_broader_than_final_premium_probability_gates():
    assert DISCOVERY_BTTS_PROBABILITY == 0.54
    assert DISCOVERY_OVER25_PROBABILITY == 0.56
    assert _market_discovery_floor("BTTS") == 0.54
    assert _market_discovery_floor("OVER_2_5") == 0.56

    tier_b = next(rule for rule in TIER_RULES if rule.name == "B")
    assert DISCOVERY_BTTS_PROBABILITY < tier_b.min_btts_probability
    assert DISCOVERY_OVER25_PROBABILITY < tier_b.min_over25_probability


def test_sprint74_default_candidate_pool_score_is_recall_oriented():
    rule = CandidatePoolRule()
    assert DISCOVERY_MIN_SCORE == 68.0
    assert rule.min_score == 68.0
    assert rule.limit >= 40


def test_sprint74_does_not_relax_final_tier_b_floors():
    tier_b = next(rule for rule in TIER_RULES if rule.name == "B")
    assert tier_b.min_btts_probability == 0.59
    assert tier_b.min_over25_probability == 0.61
    assert tier_b.min_edge == 0.05
