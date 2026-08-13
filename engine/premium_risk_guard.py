from __future__ import annotations

from dataclasses import dataclass
from django.db.models import Q
from .competition_quality import classify_competition
from .models import Fixture, Prediction, Team


@dataclass(frozen=True)
class PremiumRiskDecision:
    blocked: bool
    code: str = ""
    detail: str = ""


class PremiumRiskGuard:
    RECENT_N = 5
    OVER25_MIN_RECENT_SIDE = 0.60
    OVER25_STRONG_ANCHOR_SIDE = 0.80
    OVER25_MIN_RECENT_COMBINED = 0.70
    OVER25_MIN_MARKET_SUPPORT = 0.65
    OVER25_MAX_RECENT_FTS_FOR_NIL_RISK = 0.40
    OVER25_STRONG_CLEAN_SHEET = 0.40
    BTTS_MIN_RECENT_SIDE = 0.50
    BTTS_MAX_RECENT_FTS = 0.40
    BTTS_STRONG_CLEAN_SHEET = 0.50
    BTTS_CURRENT_ATTACK_MIN_AVG_GF = 0.80
    BTTS_CURRENT_ATTACK_MAX_FTS = 0.40

    VERIFIED_ROLE_OVERRIDES = {
        ("leagues cup", 2026, "club america", "austin"): ("austin", "club america"),
        ("leagues cup", 2026, "club america", "austin fc"): ("austin fc", "club america"),
        ("leagues cup", 2026, "club américa", "austin"): ("austin", "club américa"),
        ("leagues cup", 2026, "club américa", "austin fc"): ("austin fc", "club américa"),
    }

    @staticmethod
    def _float(evidence: dict, key: str, default=None):
        try:
            value = evidence.get(key, default)
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _norm(value) -> str:
        return " ".join(str(value or "").strip().lower().split())

    @classmethod
    def _role_mismatch(cls, fixture: Fixture):
        comp = cls._norm(fixture.competition)
        home = cls._norm(fixture.home_team.name)
        away = cls._norm(fixture.away_team.name)
        for (token, season, stored_home, stored_away), (real_home, real_away) in cls.VERIFIED_ROLE_OVERRIDES.items():
            if token in comp and fixture.season == season and home == stored_home and away == stored_away:
                return PremiumRiskDecision(True, "fixture_venue_role_mismatch", f"verified home={real_home}, away={real_away}; stored home={home}, away={away}")
        return None

    @classmethod
    def _current_attack_profile(cls, team: Team, before_fixture: Fixture) -> dict | None:
        fixtures = (Fixture.objects.filter(kickoff__lt=before_fixture.kickoff, home_goals__isnull=False, away_goals__isnull=False)
                    .filter(Q(home_team=team) | Q(away_team=team))
                    .select_related("home_team", "away_team", "competition_ref").order_by("-kickoff"))
        goals_for = []
        for fixture in fixtures.iterator(chunk_size=50):
            if classify_competition(fixture).excluded:
                continue
            goals_for.append(int(fixture.home_goals or 0) if fixture.home_team_id == team.id else int(fixture.away_goals or 0))
            if len(goals_for) >= cls.RECENT_N:
                break
        if len(goals_for) < cls.RECENT_N:
            return None
        return {"n": len(goals_for), "avg_goals_for": sum(goals_for) / len(goals_for), "failed_to_score_rate": sum(v == 0 for v in goals_for) / len(goals_for)}

    @classmethod
    def evaluate(cls, prediction: Prediction) -> PremiumRiskDecision:
        fixture = getattr(prediction, "fixture", None)
        if fixture is not None:
            mismatch = cls._role_mismatch(fixture)
            if mismatch:
                return mismatch

        evidence = (prediction.reasons or {}).get("deep_analysis_evidence") or {}
        try:
            home_n = int(evidence.get("home_recent_n") or 0)
            away_n = int(evidence.get("away_recent_n") or 0)
        except (TypeError, ValueError):
            home_n = away_n = 0

        if home_n < cls.RECENT_N or away_n < cls.RECENT_N:
            return PremiumRiskDecision(True, "venue_evidence_incomplete", f"home={home_n}/5 away={away_n}/5")

        if prediction.market == "OVER_2_5":
            h = cls._float(evidence, "home_recent_over25_rate")
            a = cls._float(evidence, "away_recent_over25_rate")
            hfts = cls._float(evidence, "home_recent_failed_to_score_rate", 0.0)
            afts = cls._float(evidence, "away_recent_failed_to_score_rate", 0.0)
            hcs = cls._float(evidence, "home_clean_sheet_rate", 0.0)
            acs = cls._float(evidence, "away_clean_sheet_rate", 0.0)
            support = cls._float(evidence, "market_support_index", 0.0)
            if h is None or a is None:
                return PremiumRiskDecision(True, "venue_over25_evidence_missing", "recent venue rates missing")
            weak_side = "home" if h <= a else "away"
            weak, strong = min(h, a), max(h, a)
            if weak < cls.OVER25_MIN_RECENT_SIDE:
                return PremiumRiskDecision(True, "venue_recent_over25_hard_floor", f"{weak_side} recent venue Over2.5 {weak:.0%} < 60%")
            if strong < cls.OVER25_STRONG_ANCHOR_SIDE:
                return PremiumRiskDecision(True, "over25_no_strong_venue_anchor", f"best venue side {strong:.0%} < 80%")
            if (h + a) / 2 < cls.OVER25_MIN_RECENT_COMBINED:
                return PremiumRiskDecision(True, "over25_recent_combined_floor", f"combined venue Over2.5 {(h+a)/2:.0%} < 70%")
            if support < cls.OVER25_MIN_MARKET_SUPPORT:
                return PremiumRiskDecision(True, "over25_market_support_hard_floor", f"support {support:.3f} < 0.65")
            if hfts >= 0.40 and acs >= 0.40:
                return PremiumRiskDecision(True, "over25_nil_risk_home", f"home FTS {hfts:.0%} + away CS {acs:.0%}")
            if afts >= 0.40 and hcs >= 0.40:
                return PremiumRiskDecision(True, "over25_nil_risk_away", f"away FTS {afts:.0%} + home CS {hcs:.0%}")
            return PremiumRiskDecision(False)

        if prediction.market == "BTTS":
            h = cls._float(evidence, "home_recent_btts_rate")
            a = cls._float(evidence, "away_recent_btts_rate")
            hfts = cls._float(evidence, "home_recent_failed_to_score_rate", 0.0)
            afts = cls._float(evidence, "away_recent_failed_to_score_rate", 0.0)
            hcs = cls._float(evidence, "home_clean_sheet_rate", 0.0)
            acs = cls._float(evidence, "away_clean_sheet_rate", 0.0)
            if h is None or a is None:
                return PremiumRiskDecision(True, "venue_btts_evidence_missing", "recent venue rates missing")
            weak_side = "home" if h <= a else "away"
            weak = min(h, a)
            if weak < cls.BTTS_MIN_RECENT_SIDE:
                return PremiumRiskDecision(True, "venue_recent_btts_hard_floor", f"{weak_side} recent venue BTTS {weak:.0%} < 50%")
            if hfts >= cls.BTTS_MAX_RECENT_FTS:
                return PremiumRiskDecision(True, "home_recent_scoring_fragility", f"home FTS {hfts:.0%} >= 40%")
            if afts >= cls.BTTS_MAX_RECENT_FTS:
                return PremiumRiskDecision(True, "away_recent_scoring_fragility", f"away FTS {afts:.0%} >= 40%")
            if hfts >= 0.20 and acs >= cls.BTTS_STRONG_CLEAN_SHEET:
                return PremiumRiskDecision(True, "btts_nil_risk_home", f"home FTS {hfts:.0%} + away CS {acs:.0%}")
            if afts >= 0.20 and hcs >= cls.BTTS_STRONG_CLEAN_SHEET:
                return PremiumRiskDecision(True, "btts_nil_risk_away", f"away FTS {afts:.0%} + home CS {hcs:.0%}")
            if fixture is not None:
                for side, team in (("home", fixture.home_team), ("away", fixture.away_team)):
                    profile = cls._current_attack_profile(team, fixture)
                    if profile and (profile["avg_goals_for"] < cls.BTTS_CURRENT_ATTACK_MIN_AVG_GF or profile["failed_to_score_rate"] >= cls.BTTS_CURRENT_ATTACK_MAX_FTS):
                        return PremiumRiskDecision(True, f"{side}_current_attack_drought", f"{side} avgGF={profile['avg_goals_for']:.2f}, FTS={profile['failed_to_score_rate']:.0%}")
        return PremiumRiskDecision(False)
