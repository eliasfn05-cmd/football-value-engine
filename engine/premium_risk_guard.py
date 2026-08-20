from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Q

from .btts_h2h_guard import h2h_metrics
from .competition_quality import classify_competition
from .models import Fixture, Prediction, Team


@dataclass(frozen=True)
class PremiumRiskDecision:
    blocked: bool
    code: str = ""
    detail: str = ""


class PremiumRiskGuard:
    """Professional Premium guard for the single operational market: BTTS YES."""

    OPERATIONAL_MARKET = "BTTS"
    RECENT_N = 5

    BTTS_MIN_RECENT_SIDE = 0.60
    BTTS_MIN_LONG_SIDE = 0.50
    BTTS_MIN_RECENT_COMBINED = 0.60
    BTTS_MAX_RECENT_FTS = 0.40
    BTTS_STRONG_CLEAN_SHEET = 0.50

    BTTS_CURRENT_ATTACK_MIN_AVG_GF = 0.90
    BTTS_CURRENT_ATTACK_MAX_FTS = 0.40

    BTTS_DEFENSIVE_SUPPRESSION_CS = 0.60
    BTTS_DEFENSIVE_SUPPRESSION_OPP_GF = 1.40
    BTTS_DEFENSIVE_SUPPRESSION_MAX_OPP_CONCEDED = 1.20

    BTTS_LOW_EVENT_MAX_COMBINED_AVG_TOTAL = 2.00
    BTTS_LOW_EVENT_MAX_RECENT_OVER25 = 0.40

    # H2H evidence is now authoritative at the risk-guard layer, therefore it
    # applies to fresh candidates, rescue candidates and publication locks.
    # We do not hard-require five H2Hs for every match, but if fewer than three
    # are stored the pick must be exceptionally strong to remain Premium.
    BTTS_H2H_MIN_EVIDENCE = 3
    BTTS_H2H_ELITE_OVERRIDE_CALIBRATED_PROB = 0.74
    BTTS_H2H_ELITE_OVERRIDE_RELIABILITY = 0.93
    BTTS_H2H_ELITE_OVERRIDE_RECENT_SIDE = 0.80
    BTTS_H2H_ELITE_OVERRIDE_LONG_SIDE = 0.60
    BTTS_H2H_ELITE_OVERRIDE_MAX_FTS = 0.20
    BTTS_H2H_HARD_RATE = 0.40

    VERIFIED_ROLE_OVERRIDES = {
        ("leagues cup", 2026, "club america", "austin"): ("austin", "club america"),
        ("leagues cup", 2026, "club america", "austin fc"): ("austin fc", "club america"),
        ("leagues cup", 2026, "club américa", "austin"): ("austin", "club américa"),
        ("leagues cup", 2026, "club américa", "austin fc"): ("austin fc", "club américa"),
    }

    VERIFIED_AUDIT_VETOES: dict[tuple[str, str, str, str], str] = {}

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
                return PremiumRiskDecision(
                    True,
                    "fixture_venue_role_mismatch",
                    f"verified home={real_home}, away={real_away}; stored home={home}, away={away}",
                )
        return None

    @classmethod
    def _verified_audit_veto(cls, prediction: Prediction):
        fixture = getattr(prediction, "fixture", None)
        kickoff = getattr(fixture, "kickoff", None) if fixture is not None else None
        if fixture is None or kickoff is None:
            return None
        target_date = kickoff.date().isoformat()
        home = cls._norm(fixture.home_team.name)
        away = cls._norm(fixture.away_team.name)
        market = str(getattr(prediction, "market", "") or "").strip().upper()
        for (date_key, home_token, away_token, market_key), detail in cls.VERIFIED_AUDIT_VETOES.items():
            direct = home_token in home and away_token in away
            reverse = home_token in away and away_token in home
            if target_date == date_key and market == market_key and (direct or reverse):
                return PremiumRiskDecision(True, "verified_audit_veto", detail)
        return None

    @classmethod
    def _current_team_profile(cls, team: Team, before_fixture: Fixture) -> dict | None:
        fixtures = (
            Fixture.objects.filter(
                kickoff__lt=before_fixture.kickoff,
                home_goals__isnull=False,
                away_goals__isnull=False,
            )
            .filter(Q(home_team=team) | Q(away_team=team))
            .select_related("home_team", "away_team", "competition_ref")
            .order_by("-kickoff")
        )
        goals_for: list[int] = []
        goals_against: list[int] = []
        over25 = 0
        btts = 0
        for fixture in fixtures.iterator(chunk_size=50):
            if classify_competition(fixture).excluded:
                continue
            if fixture.home_team_id == team.id:
                gf, ga = int(fixture.home_goals or 0), int(fixture.away_goals or 0)
            else:
                gf, ga = int(fixture.away_goals or 0), int(fixture.home_goals or 0)
            goals_for.append(gf)
            goals_against.append(ga)
            over25 += int(gf + ga >= 3)
            btts += int(gf > 0 and ga > 0)
            if len(goals_for) >= cls.RECENT_N:
                break
        if len(goals_for) < cls.RECENT_N:
            return None
        n = len(goals_for)
        return {
            "n": n,
            "avg_goals_for": sum(goals_for) / n,
            "avg_goals_against": sum(goals_against) / n,
            "avg_total_goals": sum(gf + ga for gf, ga in zip(goals_for, goals_against)) / n,
            "failed_to_score_rate": sum(v == 0 for v in goals_for) / n,
            "clean_sheet_rate": sum(v == 0 for v in goals_against) / n,
            "over25_rate": over25 / n,
            "btts_rate": btts / n,
        }

    @classmethod
    def _h2h_risk(
        cls,
        prediction: Prediction,
        *,
        h_recent: float,
        a_recent: float,
        h_long: float,
        a_long: float,
        h_fts: float,
        a_fts: float,
    ) -> PremiumRiskDecision | None:
        metrics = h2h_metrics(prediction)
        sample = int(metrics.get("sample") or 0)
        rate = float(metrics.get("btts_rate") or 0.0) if sample else None
        recent3_all_no = bool(metrics.get("recent3_all_no"))

        # Strong stored contradiction: this is a hard veto. The threshold is
        # deliberately stricter than a soft ranking penalty because Premium is
        # our highest-confidence publication layer.
        if sample >= 5 and rate is not None and rate <= cls.BTTS_H2H_HARD_RATE and recent3_all_no:
            return PremiumRiskDecision(
                True,
                "btts_h2h_hard_contradiction",
                f"H2H n={sample}, BTTS={rate:.0%}, last3=NO",
            )

        if sample in {3, 4} and rate is not None and rate <= (1.0 / 3.0) and recent3_all_no:
            return PremiumRiskDecision(
                True,
                "btts_h2h_recent_contradiction",
                f"H2H n={sample}, BTTS={rate:.0%}, last3=NO",
            )

        if sample >= cls.BTTS_H2H_MIN_EVIDENCE:
            return None

        # If our own database does not contain enough H2H to verify the market,
        # do not silently assume neutral 50%. Only an elite, independently strong
        # BTTS profile may override missing H2H evidence.
        calibration = None
        try:
            from .probability_calibration import ProbabilityEVCalibrationService
            calibration = ProbabilityEVCalibrationService().calibrate(prediction)
        except Exception:
            calibration = None

        calibrated_probability = float(getattr(calibration, "calibrated_probability", 0.0) or 0.0)
        reliability = float(getattr(calibration, "reliability", 0.0) or 0.0)
        elite_override = (
            calibrated_probability >= cls.BTTS_H2H_ELITE_OVERRIDE_CALIBRATED_PROB
            and reliability >= cls.BTTS_H2H_ELITE_OVERRIDE_RELIABILITY
            and min(h_recent, a_recent) >= cls.BTTS_H2H_ELITE_OVERRIDE_RECENT_SIDE
            and min(h_long, a_long) >= cls.BTTS_H2H_ELITE_OVERRIDE_LONG_SIDE
            and max(h_fts, a_fts) <= cls.BTTS_H2H_ELITE_OVERRIDE_MAX_FTS
        )
        if not elite_override:
            return PremiumRiskDecision(
                True,
                "btts_h2h_evidence_incomplete",
                f"stored H2H={sample}<3; elite override requires p_cal>=74%, rel>=93%, recent BTTS>=80% both",
            )
        return None

    @classmethod
    def evaluate(cls, prediction: Prediction) -> PremiumRiskDecision:
        if str(getattr(prediction, "market", "") or "").strip().upper() != cls.OPERATIONAL_MARKET:
            return PremiumRiskDecision(True, "btts_only_market", "Premium operates BTTS YES only")

        fixture = getattr(prediction, "fixture", None)
        if fixture is not None:
            if classify_competition(fixture).excluded:
                quality = classify_competition(fixture)
                return PremiumRiskDecision(True, "competition_excluded", quality.reason)
            veto = cls._verified_audit_veto(prediction)
            if veto:
                return veto
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

        h_recent = cls._float(evidence, "home_recent_btts_rate")
        a_recent = cls._float(evidence, "away_recent_btts_rate")
        h_long = cls._float(evidence, "home_btts_rate")
        a_long = cls._float(evidence, "away_btts_rate")
        h_fts = cls._float(evidence, "home_recent_failed_to_score_rate", 0.0)
        a_fts = cls._float(evidence, "away_recent_failed_to_score_rate", 0.0)
        h_cs = cls._float(evidence, "home_clean_sheet_rate", 0.0)
        a_cs = cls._float(evidence, "away_clean_sheet_rate", 0.0)

        if None in {h_recent, a_recent, h_long, a_long}:
            return PremiumRiskDecision(True, "venue_btts_evidence_missing", "BTTS venue rates missing")

        weak_recent = min(h_recent, a_recent)
        if weak_recent < cls.BTTS_MIN_RECENT_SIDE:
            side = "home" if h_recent <= a_recent else "away"
            return PremiumRiskDecision(True, "venue_recent_btts_hard_floor", f"{side} BTTS {weak_recent:.0%} < 60%")
        if min(h_long, a_long) < cls.BTTS_MIN_LONG_SIDE:
            side = "home" if h_long <= a_long else "away"
            return PremiumRiskDecision(True, "venue_long_btts_hard_floor", f"{side} long BTTS {min(h_long, a_long):.0%} < 50%")
        if (h_recent + a_recent) / 2 < cls.BTTS_MIN_RECENT_COMBINED:
            return PremiumRiskDecision(True, "btts_recent_combined_floor", "combined recent BTTS below 60%")
        if h_fts >= cls.BTTS_MAX_RECENT_FTS:
            return PremiumRiskDecision(True, "home_recent_scoring_fragility", f"home FTS {h_fts:.0%} >= 40%")
        if a_fts >= cls.BTTS_MAX_RECENT_FTS:
            return PremiumRiskDecision(True, "away_recent_scoring_fragility", f"away FTS {a_fts:.0%} >= 40%")
        if h_fts >= 0.20 and a_cs >= cls.BTTS_STRONG_CLEAN_SHEET:
            return PremiumRiskDecision(True, "btts_nil_risk_home", f"home FTS {h_fts:.0%} + away CS {a_cs:.0%}")
        if a_fts >= 0.20 and h_cs >= cls.BTTS_STRONG_CLEAN_SHEET:
            return PremiumRiskDecision(True, "btts_nil_risk_away", f"away FTS {a_fts:.0%} + home CS {h_cs:.0%}")

        if fixture is not None:
            h2h_risk = cls._h2h_risk(
                prediction,
                h_recent=h_recent,
                a_recent=a_recent,
                h_long=h_long,
                a_long=a_long,
                h_fts=h_fts,
                a_fts=a_fts,
            )
            if h2h_risk:
                return h2h_risk

            home = cls._current_team_profile(fixture.home_team, fixture)
            away = cls._current_team_profile(fixture.away_team, fixture)
            if home and away:
                for side, profile in (("home", home), ("away", away)):
                    if profile["avg_goals_for"] < cls.BTTS_CURRENT_ATTACK_MIN_AVG_GF:
                        return PremiumRiskDecision(
                            True,
                            f"{side}_current_attack_drought",
                            f"{side} avgGF={profile['avg_goals_for']:.2f}<0.90",
                        )
                    if profile["failed_to_score_rate"] >= cls.BTTS_CURRENT_ATTACK_MAX_FTS:
                        return PremiumRiskDecision(
                            True,
                            f"{side}_current_attack_drought",
                            f"{side} FTS={profile['failed_to_score_rate']:.0%}>=40%",
                        )

                if (
                    home["clean_sheet_rate"] >= cls.BTTS_DEFENSIVE_SUPPRESSION_CS
                    and away["avg_goals_for"] < cls.BTTS_DEFENSIVE_SUPPRESSION_OPP_GF
                    and home["avg_goals_against"] <= cls.BTTS_DEFENSIVE_SUPPRESSION_MAX_OPP_CONCEDED
                ):
                    return PremiumRiskDecision(
                        True,
                        "btts_home_defensive_suppression",
                        f"home CS={home['clean_sheet_rate']:.0%}, away avgGF={away['avg_goals_for']:.2f}",
                    )
                if (
                    away["clean_sheet_rate"] >= cls.BTTS_DEFENSIVE_SUPPRESSION_CS
                    and home["avg_goals_for"] < cls.BTTS_DEFENSIVE_SUPPRESSION_OPP_GF
                    and away["avg_goals_against"] <= cls.BTTS_DEFENSIVE_SUPPRESSION_MAX_OPP_CONCEDED
                ):
                    return PremiumRiskDecision(
                        True,
                        "btts_away_defensive_suppression",
                        f"away CS={away['clean_sheet_rate']:.0%}, home avgGF={home['avg_goals_for']:.2f}",
                    )

                combined_total = (home["avg_total_goals"] + away["avg_total_goals"]) / 2.0
                combined_over = (home["over25_rate"] + away["over25_rate"]) / 2.0
                if (
                    combined_total < cls.BTTS_LOW_EVENT_MAX_COMBINED_AVG_TOTAL
                    and combined_over <= cls.BTTS_LOW_EVENT_MAX_RECENT_OVER25
                    and min(h_recent, a_recent) <= 0.60
                ):
                    return PremiumRiskDecision(
                        True,
                        "btts_low_event_environment",
                        f"avgTotal={combined_total:.2f}, recent O2.5={combined_over:.0%}",
                    )

        return PremiumRiskDecision(False)
