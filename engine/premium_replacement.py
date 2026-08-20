from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .competition_quality import classify_competition
from .models import DailyPremiumSelection, Prediction, PremiumPublicationLedger
from .premium_risk_guard import PremiumRiskGuard
from .premium_selection import DYNAMIC_SCORE_FLOORS, DailyPremiumSelector
from .score_v8 import V8_MODEL_VERSION
from .value_policy import PREMIUM_MIN_EV

NON_OPERATIONAL_FIXTURE_STATUSES = {
    "1h", "ht", "2h", "et", "bt", "p", "live",
    "ft", "aet", "pen", "susp", "suspended", "int", "interrupted",
    "pst", "postponed", "canc", "cancelled", "canceled", "abd", "abandoned",
    "awd", "awarded", "wo", "walkover",
}

PREMIUM_MAX_MODEL_CALIBRATION_GAP = 0.20
PREMIUM_MIN_RAW_EV = -0.10

# Sprint 7.12.5 - guarded confidence rescue.
# 76 remains the normal selector floor, but it is no longer a universal veto.
# A 68-75.9 candidate may remain/enter Premium B only when every confidence
# signal is unusually strong and the venue-specific risk guard is clean.
PREMIUM_STANDARD_MIN_SCORE = min(DYNAMIC_SCORE_FLOORS)
PREMIUM_HARD_MIN_SCORE = 68.0
PREMIUM_RESCUE_MIN_CALIBRATED_PROBABILITY = 0.64
PREMIUM_RESCUE_MIN_CALIBRATED_EDGE = 0.065
PREMIUM_RESCUE_MIN_RELIABLE_EV = 0.10
PREMIUM_RESCUE_MIN_RELIABILITY = 0.85
PREMIUM_RESCUE_MAX_MODEL_CALIBRATION_GAP = 0.05


def normalized_fixture_status(value: str | None) -> str:
    return str(value or "").strip().lower().replace("_", " ")


def fixture_is_operational(fixture) -> bool:
    if fixture.kickoff <= timezone.now():
        return False
    return normalized_fixture_status(fixture.status) not in NON_OPERATIONAL_FIXTURE_STATUSES


class PremiumReplacementService:
    """Maintain up to three currently actionable Premium picks.

    Ordinary publication stability is preserved, while deterministic critical
    contradictions still have veto authority. Sprint 7.12.5 replaces the rigid
    score>=76 publication veto with a guarded Premium-B rescue band: a score in
    [68, 76) survives only with strong calibrated probability, edge, EV,
    reliability, model/calibration agreement and a clean Sprint 7.11 venue
    profile. This keeps strong picks such as a high-reliability 71-point profile
    without reopening weak 67-point or contradictory candidates.
    """

    def __init__(self, *, model_version: str = V8_MODEL_VERSION, max_picks: int = 3):
        self.model_version = model_version
        self.max_picks = max(1, min(int(max_picks), 3))
        self.selector = DailyPremiumSelector(model_version=model_version, max_picks=self.max_picks)

    @staticmethod
    def _publication_snapshot(row, calibration) -> dict:
        prediction = row.prediction
        fixture = prediction.fixture
        return {
            "fixture_id": fixture.id,
            "fixture_external_id": fixture.external_id,
            "home_team": fixture.home_team.name,
            "away_team": fixture.away_team.name,
            "kickoff": fixture.kickoff.isoformat(),
            "fixture_status_at_publication": fixture.status,
            "market": prediction.market,
            "selection": prediction.selection,
            "odds": float(prediction.market_odds),
            "raw_probability": calibration.raw_probability,
            "calibrated_probability": calibration.calibrated_probability,
            "implied_probability": calibration.implied_probability,
            "raw_ev": calibration.raw_ev,
            "calibrated_edge": calibration.calibrated_edge,
            "reliable_ev": calibration.reliable_ev,
            "reliability": calibration.reliability,
            "score": float(prediction.score),
            "premium_tier": row.premium_tier,
            "premium_rank_score": float(row.premium_rank_score),
            "rationale": row.rationale or {},
        }

    def _score_rescue_eligible(self, prediction: Prediction) -> tuple[bool, str]:
        score = float(prediction.score or 0.0)
        if score >= PREMIUM_STANDARD_MIN_SCORE:
            return True, "standard_score_floor"
        if score < PREMIUM_HARD_MIN_SCORE:
            return False, f"score:{score:.1f}<{PREMIUM_HARD_MIN_SCORE:.1f}"

        calibration = self.selector.calibrator.calibrate(prediction)
        gap = abs(calibration.raw_probability - calibration.calibrated_probability)
        risk = PremiumRiskGuard.evaluate(prediction)
        failures = []
        if calibration.calibrated_probability < PREMIUM_RESCUE_MIN_CALIBRATED_PROBABILITY:
            failures.append(
                f"calibrated_probability:{calibration.calibrated_probability:.3f}"
                f"<{PREMIUM_RESCUE_MIN_CALIBRATED_PROBABILITY:.3f}"
            )
        if calibration.calibrated_edge < PREMIUM_RESCUE_MIN_CALIBRATED_EDGE:
            failures.append(
                f"calibrated_edge:{calibration.calibrated_edge:.3f}"
                f"<{PREMIUM_RESCUE_MIN_CALIBRATED_EDGE:.3f}"
            )
        if calibration.reliable_ev < PREMIUM_RESCUE_MIN_RELIABLE_EV:
            failures.append(
                f"reliable_ev:{calibration.reliable_ev:.3f}"
                f"<{PREMIUM_RESCUE_MIN_RELIABLE_EV:.3f}"
            )
        if calibration.reliability < PREMIUM_RESCUE_MIN_RELIABILITY:
            failures.append(
                f"reliability:{calibration.reliability:.3f}"
                f"<{PREMIUM_RESCUE_MIN_RELIABILITY:.3f}"
            )
        if gap > PREMIUM_RESCUE_MAX_MODEL_CALIBRATION_GAP:
            failures.append(
                f"model_calibration_gap:{gap:.3f}"
                f">{PREMIUM_RESCUE_MAX_MODEL_CALIBRATION_GAP:.3f}"
            )
        if risk.blocked:
            failures.append(f"risk_guard:{risk.code}:{risk.detail}")

        if failures:
            return False, ";".join(failures)
        return True, "guarded_confidence_rescue"

    def _critical_consistency_risk(self, prediction: Prediction) -> tuple[bool, str]:
        quality = classify_competition(prediction.fixture)
        if quality.excluded:
            return True, f"competition_excluded:{quality.reason}"

        calibration = self.selector.calibrator.calibrate(prediction)
        gap = abs(calibration.raw_probability - calibration.calibrated_probability)
        if gap >= PREMIUM_MAX_MODEL_CALIBRATION_GAP:
            return True, f"model_calibration_gap:{gap:.3f}"
        if calibration.raw_ev < PREMIUM_MIN_RAW_EV:
            return True, f"raw_ev:{calibration.raw_ev:.3f}"

        rescue_ok, rescue_detail = self._score_rescue_eligible(prediction)
        if not rescue_ok:
            current_score = float(prediction.score or 0.0)
            return True, (
                f"score_quality_gate:{current_score:.1f};{rescue_detail}"
            )
        return False, ""

    @staticmethod
    def _publication_critical_risk(publication: PremiumPublicationLedger) -> tuple[bool, str]:
        snapshot = publication.snapshot or {}
        try:
            raw_probability = float(snapshot.get("raw_probability"))
            calibrated_probability = float(snapshot.get("calibrated_probability"))
            gap = abs(raw_probability - calibrated_probability)
            if gap >= PREMIUM_MAX_MODEL_CALIBRATION_GAP:
                return True, f"publication_model_calibration_gap:{gap:.3f}"
        except (TypeError, ValueError):
            pass
        try:
            raw_ev = snapshot.get("raw_ev")
            if raw_ev is not None and float(raw_ev) < PREMIUM_MIN_RAW_EV:
                return True, f"publication_raw_ev:{float(raw_ev):.3f}"
        except (TypeError, ValueError):
            pass
        return False, ""

    def _publication_or_current_critical_risk(self, publication: PremiumPublicationLedger) -> tuple[bool, str]:
        snapshot_blocked, snapshot_reason = self._publication_critical_risk(publication)
        current_blocked, current_reason = self._critical_consistency_risk(publication.prediction)
        if snapshot_blocked:
            return True, snapshot_reason
        if current_blocked:
            return True, f"current_{current_reason}"
        return False, ""

    def _rank_operational_candidates(self, target_date: date):
        start, end = self.selector._bounds(target_date)
        future_start = max(start, timezone.now())
        candidates = list(
            Prediction.objects.select_related(
                "fixture", "fixture__home_team", "fixture__away_team", "fixture__competition_ref",
            ).filter(
                model_version=self.model_version,
                fixture__kickoff__gte=future_start,
                fixture__kickoff__lt=end,
                market_odds__gte=Decimal("1.60"),
                market_odds__lte=Decimal("2.40"),
                edge__isnull=False,
                expected_value__gte=PREMIUM_MIN_EV,
            )
        )
        candidates = [p for p in candidates if fixture_is_operational(p.fixture)]

        ranked = []
        selected_floor = DYNAMIC_SCORE_FLOORS[-1]
        for score_floor in DYNAMIC_SCORE_FLOORS:
            ranked = self.selector._rank_candidates(candidates, score_floor)
            ranked = [item for item in ranked if not PremiumRiskGuard.evaluate(item[0]).blocked]
            ranked = [item for item in ranked if not self._critical_consistency_risk(item[0])[0]]
            selected_floor = score_floor
            if self.selector._unique_fixture_count(ranked) >= self.max_picks:
                break

        # If the normal >=76 selector cannot fill the card, consider only the
        # tightly constrained 68-75.9 confidence-rescue profiles. This does not
        # weaken the standard pool and never forces three picks.
        if self.selector._unique_fixture_count(ranked) < self.max_picks:
            existing_ids = {item[0].id for item in ranked}
            for prediction in candidates:
                score = float(prediction.score or 0.0)
                if prediction.id in existing_ids:
                    continue
                if not (PREMIUM_HARD_MIN_SCORE <= score < PREMIUM_STANDARD_MIN_SCORE):
                    continue
                if not self.selector._passes_hard_value_floors(prediction):
                    continue
                if PremiumRiskGuard.evaluate(prediction).blocked:
                    continue
                rescue_ok, rescue_detail = self._score_rescue_eligible(prediction)
                if not rescue_ok:
                    continue
                if self._critical_consistency_risk(prediction)[0]:
                    continue
                rank_score, rationale = self.selector._rank_score(prediction)
                rationale = dict(rationale or {})
                rationale["premium_tier"] = "B"
                rationale["premium_score_rescue"] = {
                    "enabled": True,
                    "detail": rescue_detail,
                    "score": score,
                    "hard_min_score": PREMIUM_HARD_MIN_SCORE,
                    "standard_min_score": PREMIUM_STANDARD_MIN_SCORE,
                }
                ranked.append((prediction, "B", rank_score, rationale))
                existing_ids.add(prediction.id)

            tier_priority = {"A": 2, "B": 1}
            ranked.sort(
                key=lambda item: (
                    tier_priority.get(item[1], 0),
                    item[2],
                    float(item[3].get("reliable_expected_value") or 0),
                    float(item[0].score or 0.0),
                ),
                reverse=True,
            )

        return ranked, selected_floor

    def _locked_publications(self, target_date: date) -> list[PremiumPublicationLedger]:
        publications = (
            PremiumPublicationLedger.objects.select_related(
                "prediction", "prediction__fixture", "prediction__fixture__home_team",
                "prediction__fixture__away_team", "prediction__fixture__competition_ref",
            )
            .filter(target_date=target_date, model_version=self.model_version)
            .order_by("published_rank", "id")
        )
        locked: list[PremiumPublicationLedger] = []
        seen_fixtures: set[int] = set()
        for publication in publications:
            fixture = publication.prediction.fixture
            if fixture.id in seen_fixtures or not fixture_is_operational(fixture):
                continue
            critical, _ = self._publication_or_current_critical_risk(publication)
            if critical:
                continue
            locked.append(publication)
            seen_fixtures.add(fixture.id)
            if len(locked) >= self.max_picks:
                break
        return locked

    @transaction.atomic
    def reconcile(self, target_date: date, *, trigger: str = "scheduled_reconcile") -> list[DailyPremiumSelection]:
        previous = list(
            DailyPremiumSelection.objects.select_related(
                "prediction", "prediction__fixture", "prediction__fixture__home_team",
                "prediction__fixture__away_team",
            )
            .filter(target_date=target_date, model_version=self.model_version)
            .order_by("rank")
        )
        previous_ids = {row.prediction_id for row in previous}

        all_publications = list(
            PremiumPublicationLedger.objects.select_related(
                "prediction", "prediction__fixture", "prediction__fixture__home_team",
                "prediction__fixture__away_team", "prediction__fixture__competition_ref",
            )
            .filter(target_date=target_date, model_version=self.model_version)
            .order_by("published_rank", "id")
        )
        locked_publications = self._locked_publications(target_date)
        locked_prediction_ids = {row.prediction_id for row in locked_publications}
        locked_fixture_ids = {row.prediction.fixture_id for row in locked_publications}

        removed_publications = []
        removed_labels = []
        for row in all_publications:
            unavailable = not fixture_is_operational(row.prediction.fixture)
            critical, critical_reason = self._publication_or_current_critical_risk(row)
            if unavailable or critical:
                removed_publications.append(row)
                label = f"{row.prediction.fixture.home_team.name} vs {row.prediction.fixture.away_team.name}"
                if critical:
                    label += f" [{critical_reason}]"
                removed_labels.append(label)

        ranked, selected_floor = self._rank_operational_candidates(target_date)

        chosen_new = []
        seen_fixtures = set(locked_fixture_ids)
        slots_left = max(0, self.max_picks - len(locked_publications))
        if slots_left:
            for item in ranked:
                prediction = item[0]
                if prediction.id in locked_prediction_ids or prediction.fixture_id in seen_fixtures:
                    continue
                chosen_new.append(item)
                seen_fixtures.add(prediction.fixture_id)
                if len(chosen_new) >= slots_left:
                    break

        DailyPremiumSelection.objects.filter(target_date=target_date, model_version=self.model_version).delete()

        rows: list[DailyPremiumSelection] = []
        for publication in locked_publications:
            snapshot = publication.snapshot or {}
            rationale = dict(snapshot.get("rationale") or {})
            rescue_ok, rescue_detail = self._score_rescue_eligible(publication.prediction)
            rationale["publication_lock"] = {
                "locked": True,
                "source": "PremiumPublicationLedger",
                "published_rank": publication.published_rank,
                "policy": "stable_unless_fixture_non_operational_or_critical_consistency_veto",
            }
            if float(publication.prediction.score or 0.0) < PREMIUM_STANDARD_MIN_SCORE:
                rationale["premium_score_rescue"] = {
                    "enabled": rescue_ok,
                    "detail": rescue_detail,
                    "score": float(publication.prediction.score or 0.0),
                    "hard_min_score": PREMIUM_HARD_MIN_SCORE,
                    "standard_min_score": PREMIUM_STANDARD_MIN_SCORE,
                }
            current_rank_score, _ = self.selector._rank_score(publication.prediction)
            rows.append(DailyPremiumSelection(
                target_date=target_date,
                prediction=publication.prediction,
                rank=len(rows) + 1,
                premium_tier=publication.premium_tier,
                premium_rank_score=Decimal(f"{current_rank_score:.2f}"),
                model_version=self.model_version,
                rationale=rationale,
            ))

        for prediction, tier, rank_score, rationale in chosen_new:
            rationale = dict(rationale or {})
            rationale["selector_dynamic_floor_used"] = selected_floor
            risk = PremiumRiskGuard.evaluate(prediction)
            critical, critical_reason = self._critical_consistency_risk(prediction)
            rescue_ok, rescue_detail = self._score_rescue_eligible(prediction)
            rationale["sprint_7_12_5_consistency_guard"] = {
                "blocked": critical,
                "detail": critical_reason,
                "max_model_calibration_gap": PREMIUM_MAX_MODEL_CALIBRATION_GAP,
                "min_raw_ev": PREMIUM_MIN_RAW_EV,
                "hard_min_score": PREMIUM_HARD_MIN_SCORE,
                "standard_min_score": PREMIUM_STANDARD_MIN_SCORE,
                "score_rescue_eligible": rescue_ok,
                "score_rescue_detail": rescue_detail,
            }
            rationale["sprint_7_11_risk_guard"] = {
                "blocked": risk.blocked, "code": risk.code, "detail": risk.detail,
            }
            rationale["publication_lock"] = {
                "locked": True,
                "source": "PremiumPublicationLedger",
                "policy": "stable_unless_fixture_non_operational_or_critical_consistency_veto",
            }
            if removed_publications:
                rationale.update({
                    "promotion_reason": "PROMOTED_AFTER_PREMIUM_REMOVAL_OR_CRITICAL_VETO",
                    "promotion_trigger": trigger,
                    "replaced_premium": removed_labels,
                    "promoted_at": timezone.now().isoformat(),
                })
            elif previous_ids:
                rationale.update({
                    "promotion_reason": "FILLED_AVAILABLE_PREMIUM_SLOT",
                    "promotion_trigger": trigger,
                    "promoted_at": timezone.now().isoformat(),
                })
            rows.append(DailyPremiumSelection(
                target_date=target_date,
                prediction=prediction,
                rank=len(rows) + 1,
                premium_tier=tier,
                premium_rank_score=Decimal(f"{rank_score:.2f}"),
                model_version=self.model_version,
                rationale=rationale,
            ))

        # Reorder the surviving set by CURRENT quality, not stale publication
        # rank. Publication lock stabilizes membership; current rank only orders
        # the cards. This fixes cases where a weaker Tier-B card was shown above
        # a stronger one after replacement/reconciliation.
        tier_priority = {"A": 2, "B": 1}
        rows.sort(
            key=lambda row: (
                tier_priority.get(row.premium_tier, 0),
                float(row.premium_rank_score or 0),
                self.selector.calibrator.calibrate(row.prediction).reliable_ev,
                float(row.prediction.score or 0.0),
            ),
            reverse=True,
        )
        for index, row in enumerate(rows, start=1):
            row.rank = index

        if rows:
            DailyPremiumSelection.objects.bulk_create(rows)

        persisted = list(
            DailyPremiumSelection.objects.select_related(
                "prediction", "prediction__fixture", "prediction__fixture__home_team",
                "prediction__fixture__away_team", "prediction__fixture__competition_ref",
            )
            .filter(target_date=target_date, model_version=self.model_version)
            .order_by("rank")
        )

        for row in persisted:
            prediction = row.prediction
            calibration = self.selector.calibrator.calibrate(prediction)
            PremiumPublicationLedger.objects.get_or_create(
                prediction=prediction,
                defaults={
                    "target_date": target_date,
                    "published_rank": row.rank,
                    "premium_tier": row.premium_tier,
                    "premium_rank_score": row.premium_rank_score,
                    "model_version": row.model_version,
                    "market": prediction.market,
                    "selection": prediction.selection,
                    "odds": prediction.market_odds,
                    "snapshot": self._publication_snapshot(row, calibration),
                },
            )
        return persisted