import hmac
import json
import os
import time
from datetime import date, datetime, time as dt_time, timedelta

from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from engine.candidate_pool import CandidatePoolRule, high_recall_candidate_pool
from engine.competition_quality import classify_competition
from engine.model_diagnostics import ModelDiagnosticsService
from engine.models import DailyPremiumSelection, Prediction
from engine.premium_risk_guard import PremiumRiskGuard
from engine.premium_selection import DailyPremiumSelector
from engine.score_v8 import V8_MODEL_VERSION
from engine.value_policy import PREMIUM_MIN_EV, PREMIUM_VALUE_MAX_ODDS, PREMIUM_VALUE_MIN_ODDS, is_premium_value_odds
from scanner.models import PremiumGenerationJob

from .pipeline_trigger import GitHubPipelineTrigger
from .services import DashboardService


def _expire_stale_generation_jobs() -> None:
    now = timezone.now()
    dispatch_cutoff = now - timedelta(minutes=15)
    running_cutoff = now - timedelta(minutes=50)
    stale_dispatched = PremiumGenerationJob.objects.filter(
        status__in=[PremiumGenerationJob.STATUS_QUEUED, PremiumGenerationJob.STATUS_DISPATCHED],
        requested_at__lt=dispatch_cutoff,
    )
    stale_running = PremiumGenerationJob.objects.filter(
        status=PremiumGenerationJob.STATUS_RUNNING,
        started_at__lt=running_cutoff,
    )
    for qs, message in (
        (stale_dispatched, "El worker no reclamó el trabajo a tiempo."),
        (stale_running, "La generación excedió el tiempo máximo permitido."),
    ):
        qs.update(
            status=PremiumGenerationJob.STATUS_FAILED,
            current_stage="TIMEOUT",
            progress_pct=100,
            message=message,
            finished_at=now,
        )


def _interactive_pipeline_mode(target_date) -> str:
    start = timezone.make_aware(datetime.combine(target_date, dt_time.min))
    end = start + timedelta(days=1)
    exists = Prediction.objects.filter(
        model_version=V8_MODEL_VERSION,
        fixture__kickoff__gte=start,
        fixture__kickoff__lt=end,
    ).exists()
    return "refresh" if exists else "full"


def _parse_target_date(raw_value, *, default_today: bool = True):
    raw = str(raw_value or "").strip()
    if not raw:
        return timezone.localdate() if default_today else None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _selected_date_picks(target_date, *, limit: int = 3):
    """Return the official active Premium rows exactly as reconciled.

    Sprint 7.12.2 stability rule: once PremiumReplacementService has published
    and locked a pick, the dashboard must not independently re-score/re-filter
    it on every page load. Re-running Deep, odds, EV or risk checks here was the
    reason an official pick could disappear minutes later without its fixture
    being suspended or cancelled.

    Admission quality is enforced before publication; after publication the
    DailyPremiumSelection + PremiumPublicationLedger pair is the source of
    truth until replacement reconciliation vacates the slot.
    """
    return list(
        DailyPremiumSelection.objects.select_related(
            "prediction",
            "prediction__fixture",
            "prediction__fixture__home_team",
            "prediction__fixture__away_team",
            "prediction__fixture__competition_ref",
        )
        .filter(target_date=target_date, model_version=V8_MODEL_VERSION)
        .order_by("rank")[:limit]
    )


def _selected_date_operational(target_date, picks):
    start = timezone.make_aware(datetime.combine(target_date, dt_time.min))
    end = start + timedelta(days=1)
    unique_fixtures = Prediction.objects.filter(
        model_version=V8_MODEL_VERSION,
        fixture__kickoff__gte=start,
        fixture__kickoff__lt=end,
    ).values("fixture_id").distinct().count()
    if picks:
        avg_ev = sum(float(row.prediction.expected_value or 0) for row in picks) / len(picks)
        avg_score = sum(float(row.prediction.score) for row in picks) / len(picks)
        avg_rank = sum(float(row.premium_rank_score) for row in picks) / len(picks)
    else:
        avg_ev = avg_score = avg_rank = None
    return {
        "fixtures_analyzed": unique_fixtures,
        "premium_count": len(picks),
        "avg_ev_pct": round(avg_ev * 100, 1) if avg_ev is not None else None,
        "avg_score": round(avg_score, 1) if avg_score is not None else None,
        "avg_rank_score": round(avg_rank, 1) if avg_rank is not None else None,
        "action": "BET" if picks else "NO_BET",
    }


def _validated_pending_odds(*, limit: int = 20):
    """Show missing odds only when the prediction is genuinely close to Premium."""
    today = timezone.localdate()
    pool = high_recall_candidate_pool(today, rule=CandidatePoolRule(limit=200), model_version=V8_MODEL_VERSION)
    prediction_ids = [entry.prediction_id for entry in pool]
    if not prediction_ids:
        return []
    qs = Prediction.objects.select_related("fixture", "fixture__home_team", "fixture__away_team", "fixture__competition_ref").filter(
        id__in=prediction_ids, model_version=V8_MODEL_VERSION, fixture__kickoff__gte=timezone.now(), market_odds__isnull=True, score__gte=70.0,
    ).order_by("-score", "-probability", "fixture__kickoff")
    rows = []
    for prediction in qs.iterator(chunk_size=100):
        if classify_competition(prediction.fixture).excluded:
            continue
        p = float(prediction.probability)
        if prediction.market == "BTTS" and p < 0.54:
            continue
        if prediction.market == "OVER_2_5" and p < 0.56:
            continue
        if prediction.market not in {"BTTS", "OVER_2_5"}:
            continue
        rows.append(prediction)
        if len(rows) >= limit:
            break
    return rows


def _risk_guard_reason(prediction: Prediction) -> str | None:
    risk = PremiumRiskGuard.evaluate(prediction)
    if not risk.blocked:
        return None
    labels = {
        "venue_recent_over25_hard_floor": "Over 2.5 bloqueado: uno de los equipos no alcanza 3/5 en su condición local/visita",
        "over25_no_strong_venue_anchor": "Over 2.5 bloqueado: falta un ancla venue fuerte de 4/5",
        "over25_recent_combined_floor": "Over 2.5 bloqueado: señal combinada local/visita inferior al 70%",
        "over25_market_support_hard_floor": "Over 2.5 bloqueado: soporte Deep del mercado insuficiente",
        "over25_nil_risk_home": "Over 2.5 bloqueado: riesgo alto de local sin marcar / rival con porterías a cero",
        "over25_nil_risk_away": "Over 2.5 bloqueado: riesgo alto de visitante sin marcar / local con porterías a cero",
        "venue_recent_btts_hard_floor": "BTTS bloqueado: uno de los equipos no alcanza el piso venue reciente",
        "home_recent_scoring_fragility": "BTTS bloqueado: fragilidad anotadora reciente del local",
        "away_recent_scoring_fragility": "BTTS bloqueado: fragilidad anotadora reciente del visitante",
        "btts_nil_risk_home": "BTTS bloqueado: riesgo de local en cero",
        "btts_nil_risk_away": "BTTS bloqueado: riesgo de visitante en cero",
        "home_current_attack_drought": "BTTS bloqueado: sequía ofensiva reciente del local",
        "away_current_attack_drought": "BTTS bloqueado: sequía ofensiva reciente del visitante",
    }
    label = labels.get(risk.code, f"Bloqueado por Sprint 7.11 ({risk.code})")
    return f"{label}: {risk.detail}" if risk.detail else label


def _human_current_rejection_reason(prediction: Prediction) -> str:
    reasons = DailyPremiumSelector.rejection_reasons(prediction, score_floor=76.0)
    risk_reason = _risk_guard_reason(prediction)
    if not reasons and not risk_reason:
        return "Cumple todos los filtros Premium"
    labels = []
    for reason in reasons:
        if reason.startswith("competition:"):
            labels.append("Competición fuera del universo profesional")
        elif reason == "v8_gates": labels.append("No supera validaciones estructurales V8")
        elif reason == "deep_missing": labels.append("Deep Analysis no disponible")
        elif reason == "deep_rejected": labels.append("Deep Analysis rechazó el perfil")
        elif reason == "not_market_eligible_deep_preferred": labels.append("Otro mercado apostable del partido tiene mejor Deep")
        elif reason == "no_odds": labels.append("Sin cuota operativa")
        elif reason == "odds_outside_1.60_2.40": labels.append("Cuota fuera de 1.60–2.40")
        elif reason.startswith("reliability:"): labels.append(f"Reliability insuficiente ({reason.split(':', 1)[1]})")
        elif reason.startswith("disagreement_reliability:"): labels.append("Reliability penalizada por desacuerdo modelo/mercado")
        elif reason.startswith("raw_probability:"):
            value = float(reason.split(":", 1)[1]); floor = 0.54 if prediction.market == "BTTS" else 0.56; label = "BTTS" if prediction.market == "BTTS" else "Over 2.5"
            labels.append(f"Probabilidad {label} {value:.1%} < piso Sprint 7.7 {floor:.0%}")
        elif reason.startswith("calibrated_edge:"): labels.append(f"Edge calibrado {float(reason.split(':', 1)[1]):.1%} < 5%")
        elif reason.startswith("reliable_ev:"): labels.append(f"EV fiable {float(reason.split(':', 1)[1]):.1%} < 3%")
        elif reason == "fragile_over25_two_goal_ceiling": labels.append("Over frágil: riesgo de techo de 2 goles")
        elif reason.startswith("score:"): labels.append(f"Score final {float(reason.split(':', 1)[1]):.1f} < piso dinámico 76")
        else: labels.append(reason)
    if risk_reason:
        labels.append(risk_reason)
    return "; ".join(labels)


def health(request):
    return JsonResponse({"status": "ok", "service": "football-value-engine", "version": "1.0.0"})


def dashboard_home(request):
    _expire_stale_generation_jobs()
    selected_date = _parse_target_date(request.GET.get("date")) or timezone.localdate()
    service = DashboardService(); context = service.build(); picks = _selected_date_picks(selected_date)
    context["premium_picks"] = picks; context["operational"] = _selected_date_operational(selected_date, picks)
    context["selected_date"] = selected_date; context["selected_date_iso"] = selected_date.isoformat(); context["today_iso"] = timezone.localdate().isoformat()
    context["pipeline_trigger_configured"] = GitHubPipelineTrigger().configured and bool(os.getenv("PIPELINE_TRIGGER_PIN", "").strip())
    active_job = PremiumGenerationJob.objects.filter(target_date=selected_date, status__in=PremiumGenerationJob.ACTIVE_STATUSES).first()
    context["active_generation_job_id"] = active_job.id if active_job else None
    return render(request, "dashboard/index.html", context)


def developer_dashboard(request):
    service = DashboardService(); context = service.build_developer()
    context["pending_odds"] = _validated_pending_odds(limit=20); context["model_diagnostics"] = ModelDiagnosticsService().build(); context["deep_premium"] = service.premium_picks(limit=3)

    # Sprint 7.12.1 — developer audit must use the exact same active date and
    # the exact same final risk guard as the operational Premium selector.
    target_date = timezone.localdate()
    start = timezone.make_aware(datetime.combine(target_date, dt_time.min))
    end = start + timedelta(days=1)
    selected_prediction_ids = set(
        DailyPremiumSelection.objects.filter(
            target_date=target_date,
            model_version=V8_MODEL_VERSION,
        ).values_list("prediction_id", flat=True)
    )
    selected_count = len(selected_prediction_ids)

    alternates, rejected = [], []
    for source_row in context.get("near_premium", []):
        prediction = source_row["prediction"]
        if not (start <= prediction.fixture.kickoff < end):
            continue
        if prediction.id in selected_prediction_ids:
            continue
        row = dict(source_row)
        reasons = DailyPremiumSelector.rejection_reasons(prediction, score_floor=76.0)
        risk = PremiumRiskGuard.evaluate(prediction)

        if not reasons and not risk.blocked:
            if selected_count >= 3:
                row["reason"] = "Cumple todos los filtros Premium; suplente por ranking global después de completar el Top 3"
                alternates.append(row)
            else:
                # This should be impossible after PremiumReplacementService.reconcile().
                # Do not mislabel it as a Top-3 alternate: surface the inconsistency.
                row["reason"] = "Elegible Premium con plaza disponible; requiere reconciliación del selector"
                rejected.append(row)
        else:
            row["reason"] = _human_current_rejection_reason(prediction)
            rejected.append(row)

    alternates.sort(key=lambda row: (float(row["prediction"].score or 0), float(row["prediction"].expected_value or 0)), reverse=True)
    rejected.sort(key=lambda row: float(row["prediction"].score or 0), reverse=True)
    context["premium_alternates"] = alternates
    context["near_premium"] = rejected
    return render(request, "dashboard/developer.html", context)


@require_POST
def generate_premium_picks(request):
    _expire_stale_generation_jobs(); expected_pin = os.getenv("PIPELINE_TRIGGER_PIN", "").strip()
    if not expected_pin: return JsonResponse({"ok": False, "message": "PIPELINE_TRIGGER_PIN no está configurado en Render."}, status=503)
    try: payload = json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError): return JsonResponse({"ok": False, "message": "Solicitud inválida."}, status=400)
    supplied_pin = str(payload.get("pin") or "").strip()
    if not hmac.compare_digest(supplied_pin, expected_pin): return JsonResponse({"ok": False, "message": "PIN incorrecto."}, status=403)
    target_date = _parse_target_date(payload.get("target_date"), default_today=False)
    if target_date is None: return JsonResponse({"ok": False, "message": "Selecciona una fecha válida para los partidos."}, status=400)
    if target_date < timezone.localdate(): return JsonResponse({"ok": False, "message": "La generación operativa solo admite hoy o fechas futuras."}, status=400)
    active_job = PremiumGenerationJob.objects.filter(target_date=target_date, status__in=PremiumGenerationJob.ACTIVE_STATUSES).first()
    if active_job: return JsonResponse({"ok": True, "already_running": True, "message": "Ya hay una generación en curso para esa fecha.", "job_id": active_job.id, "target_date": active_job.target_date.isoformat()})
    now_ts = time.time(); last_trigger = float(request.session.get("premium_trigger_ts", 0) or 0)
    if now_ts - last_trigger < 45: return JsonResponse({"ok": False, "message": "Espera unos segundos antes de volver a generar."}, status=429)
    mode = _interactive_pipeline_mode(target_date)
    job = PremiumGenerationJob.objects.create(target_date=target_date, mode=mode, status=PremiumGenerationJob.STATUS_QUEUED, current_stage="QUEUE", progress_pct=0, message=f"Solicitud creada para {target_date.isoformat()}; enviando al worker.", metadata={"source": "dashboard", "interactive_fast": True, "resolved_mode": mode, "selected_date": target_date.isoformat()})
    result = GitHubPipelineTrigger().dispatch(target_date=target_date, mode=mode, generation_job_id=job.id)
    if not result.accepted:
        job.status = PremiumGenerationJob.STATUS_FAILED; job.current_stage = "DISPATCH"; job.progress_pct = 100; job.message = result.message[:255]; job.finished_at = timezone.now(); job.save(update_fields=["status", "current_stage", "progress_pct", "message", "finished_at"])
        return JsonResponse({"ok": False, "message": result.message, "job_id": job.id}, status=502)
    job.status = PremiumGenerationJob.STATUS_DISPATCHED; job.current_stage = "QUEUE"; job.progress_pct = 1; job.message = f"En cola para {target_date.isoformat()}; modo {mode}, esperando worker de GitHub Actions."; job.dispatched_at = timezone.now(); job.save(update_fields=["status", "current_stage", "progress_pct", "message", "dispatched_at"]); request.session["premium_trigger_ts"] = now_ts
    return JsonResponse({"ok": True, "message": result.message, "job_id": job.id, "target_date": target_date.isoformat(), "mode": mode})


@require_GET
def premium_generation_status(request):
    _expire_stale_generation_jobs(); raw_job_id = request.GET.get("job_id"); job = None
    if raw_job_id:
        try: job = PremiumGenerationJob.objects.select_related("pipeline").filter(pk=int(raw_job_id)).first()
        except (TypeError, ValueError): return JsonResponse({"ok": False, "message": "job_id inválido."}, status=400)
    if job is None: job = PremiumGenerationJob.objects.select_related("pipeline").first()
    if job is None: return JsonResponse({"ok": True, "job": None, "premium_count": 0, "stages": []})
    stages = []
    if job.pipeline_id: stages = [{"name": stage.name, "status": stage.status, "message": stage.message, "duration_seconds": stage.duration_seconds, "records_processed": stage.records_processed} for stage in job.pipeline.stages.all()]
    # Count the reconciled official rows directly. Re-applying odds/EV filters
    # here made the progress widget disagree with the locked dashboard after a
    # later market refresh.
    premium_count = DailyPremiumSelection.objects.filter(
        target_date=job.target_date,
        model_version=V8_MODEL_VERSION,
    ).count()
    return JsonResponse({"ok": True, "job": {"id": job.id, "status": job.status, "target_date": job.target_date.isoformat(), "current_stage": job.current_stage, "progress_pct": job.progress_pct, "message": job.message, "pipeline_id": job.pipeline_id, "finished": job.finished_at is not None, "mode": job.mode}, "premium_count": premium_count, "stages": stages})
