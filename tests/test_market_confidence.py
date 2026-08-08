from types import SimpleNamespace
from unittest.mock import patch

from engine.features import FeatureVector, VenueProfile
from engine.market_confidence import MarketConfidenceService


def _features(*, away_over=0.20, away_gf=0.70, away_ga=1.10, home_over=0.60):
    home = VenueProfile(
        sample_size=5,
        goals_for=1.8,
        goals_against=1.1,
        over25_rate=home_over,
        btts_rate=0.60,
        clean_sheet_rate=0.20,
        failed_to_score_rate=0.20,
    )
    away = VenueProfile(
        sample_size=5,
        goals_for=away_gf,
        goals_against=away_ga,
        over25_rate=away_over,
        btts_rate=0.40,
        clean_sheet_rate=0.20,
        failed_to_score_rate=0.40,
    )
    return FeatureVector(
        fixture_id="context-test",
        home_team="Santiago Wanderers",
        away_team="San Marcos de Arica",
        home_profile=home,
        away_profile=away,
        home_over25_last5_home=home.over25_rate,
        away_over25_last5_away=away.over25_rate,
        home_btts_last5_home=home.btts_rate,
        away_btts_last5_away=away.btts_rate,
        home_clean_sheet_rate=home.clean_sheet_rate,
        away_clean_sheet_rate=away.clean_sheet_rate,
        home_failed_to_score_rate=home.failed_to_score_rate,
        away_failed_to_score_rate=away.failed_to_score_rate,
        home_table_position=2,
        away_table_position=8,
        home_points_per_game=1.8,
        away_points_per_game=1.1,
        home_lineup_continuity=None,
        away_lineup_continuity=None,
        btts_market_odds=2.0,
        over25_market_odds=2.05,
        data_quality_score=80.0,
    )


def test_over25_rejected_when_away_pattern_and_context_are_closed():
    service = MarketConfidenceService()
    fixture = SimpleNamespace()
    with patch.object(service, "_h2h", return_value=(6, 0.33, 0.50)), patch.object(
        service, "_competition", return_value=(40, 0.42, 0.50)
    ):
        result = service.evaluate(fixture, _features(), "OVER_2_5")

    assert result.passed is False
    assert result.score < 50
    assert "away_over25_very_low" in result.failures
    assert "away_total_goals_very_low" in result.failures
    assert "h2h_over25_low" in result.failures
    assert "competition_over25_low" in result.failures
    assert result.evidence["away_over25_rate"] == 0.20
    assert result.evidence["away_avg_total_goals"] == 1.8


def test_over25_passes_when_both_venue_profiles_support_market():
    service = MarketConfidenceService()
    fixture = SimpleNamespace()
    features = _features(away_over=0.60, away_gf=1.4, away_ga=1.4, home_over=0.65)
    with patch.object(service, "_h2h", return_value=(6, 0.67, 0.50)), patch.object(
        service, "_competition", return_value=(40, 0.55, 0.50)
    ):
        result = service.evaluate(fixture, features, "OVER_2_5")

    assert result.passed is True
    assert result.score >= 50
    assert "market_confidence_below_50" not in result.failures
