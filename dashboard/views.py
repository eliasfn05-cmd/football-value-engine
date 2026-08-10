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
    service = DashboardService()
    rows = []
    candidates = (
        DailyPremiumSelection.objects.select_related(
            "prediction",
            "prediction__fixture",
            "prediction__fixture__home_team",
            "prediction__fixture__away_team",
            "prediction__fixture__competition_ref",
        )
        .filter(target_date=target_date, model_version=V8_MODEL_VERSION)
        .order_by("rank")
    )
    for row in candidates.iterator(chunk_size=20):
        prediction = row.prediction
        if classify_competition(prediction.fixture).excluded:
            continue
        if not is_premium_value_odds(prediction.market_odds):
            continue
        if prediction.expected_value is None or prediction.expected_value < PREMIUM_MIN_EV:
            continue
        deep = service._deep_state(prediction)
        if deep.get("status") != "complete" or deep.get("passed") is not True:
            continue
        # Sprint 7.7: a non-original Deep preferred market may still be the
        # operational choice when the preferred sibling is outside 1.60-2.40.
        if not DailyPremiumSelector._market_eligible_deep_preference(prediction):
            continue
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


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
    today = timezone.localdate()
    pool = high_recall_candidate_pool(
        today,
        rule=CandidatePoolRule(limit=200),
        model_version=V8_MODEL_VERSION,
    )
    prediction_ids = [entry.prediction_id for entry in pool]
    if not prediction_ids:
        return []
    rows = (
        Prediction.objects.select_related(
            "fixture",
            "fixture__home_team",
            "fixture__away_team",
            "fixture__competition_ref",
        )
        .filter(
            id__in=prediction_ids,
            model_version=V8_MODEL_VERSION,
            fixture__kickoff__gte=timezone.now(),
            market_odds__isnull=True,
        )
        .order_by("-score", "-probability", "fixture__kickoff")
    )
    return list(rows[:limit])


def _human_current_rejection_reason(prediction: Prediction) -> str:
    """Translate Sprint 7.7/7.8 selector diagnostics into dashboard language."""
    reasons = DailyPremiumSelector.rejection_reasons(prediction, score_floor=76.0)
    if not reasons:
        return "Cumple filtros Premium; quedó fuera solo por ranking del Top 3"

    labels = []
    for reason in reasons:
        if reason.startswith("competition:"):
            labels.append("Competición fuera del universo profesional")
        elif reason == "v8_gates":
            labels.append("No supera validaciones estructurales V8")
        elif reason == "deep_missing":
            labels.append("Deep Analysis no disponible")
        elif reason == "deep_rejected":
            labels.append("Deep Analysis rechazó el perfil")
        elif reason == "not_market_eligible_deep_preferred":
            labels.append("Otro mercado apostable del partido tiene mejor Deep")
        elif reason == "no_odds":
            labels.append("Sin cuota operativa")
        elif reason == "odds_outside_1.60_2.40":
            labels.append("Cuota fuera de 1.60–2.40")
        elif reason.startswith("reliability:"):
            value = reason.split(":", 1)[1]
            labels.append(f"Reliability insuficiente ({value})")
        elif reason.startswith("disagreement_reliability:"):
            labels.append("Reliability penalizada por desacuerdo modelo/mercado")
        elif reason.startswith("raw_probability:"):
            value = float(reason.split(":", 1)[1])
            floor = 0.54 if prediction.market == "BTTS" else 0.56
            label = "BTTS" if prediction.market == "BTTS" else "Over 2.5"
            labels.append(f"Probabilidad {label} {value:.1%} < piso Sprint 7.7 {floor:.0%}")
        elif reason.startswith("calibrated_edge:"):
            value = float(reason.split(":", 1)[1])
            labels.append(f"Edge calibrado {value:.1%} < 5%")
        elif reason.startswith("reliable_ev:"):
            value = float(reason.split(":", 1)[1])
            labels.append(f"EV fiable {value:.1%} < 3%")
        elif reason == "fragile_over25_two_goal_ceiling":
            labels.append("Over frágil: riesgo de techo de 2 goles")
        elif reason.startswith("score:"):
            value = float(reason.split(":", 1)[1])
            labels.append(f"Score final {value:.1f} < piso dinámico 76")
        else:
            labels.append(reason)
    return "; ".join(labels)


def health(request):
    return JsonResponse({"status": "ok", "service": "football-value-engine", "version": "1.0.0"})


def dashboard_home(request):
    _expire_stale_generation_jobs()
    selected_date = _parse_target_date(request.GET.get("date")) or timezone.localdate()
    service = DashboardService()
    context = service.build()
    picks = _selected_date_picks(selected_date)
    context["premium_picks"] = picks
    context["operational"] = _selected_date_operational(selected_date, picks)
    context["selected_date"] = selected_date
    context["selected_date_iso"] = selected_date.isoformat()
    context["today_iso"] = timezone.localdate().isoformat()
    context["pipeline_trigger_configured"] = GitHubPipelineTrigger().configured and bool(
        os.getenv("PIPELINE_TRIGGER_PIN", "").strip()
    )
    active_job = PremiumGenerationJob.objects.filter(
        target_date=selected_date,
        status__in=PremiumGenerationJob.ACTIVE_STATUSES,
    ).first()
    context["active_generation_job_id"] = active_job.id if active_job else None
    return render(request, "dashboard/index.html", context)


def developer_dashboard(request):
    service = DashboardService()
    context = service.build_developer()
    context["pending_odds"] = _validated_pending_odds(limit=20)
    context["model_diagnostics"] = ModelDiagnosticsService().build()
    context["deep_premium"] = service.premium_picks(limit=3)

    # Sprint 7.8.1: a selected Premium must never reappear as a rejected/near
    # candidate. Also replace legacy 59/61/raw-Edge explanations with the exact
    # current Sprint 7.7 selector diagnostics (calibrated Edge, reliable EV,
    # effective reliability, market-eligible Deep and dynamic score floor).
    selected_prediction_ids = set(
        DailyPremiumSelection.objects.filter(
            model_version=V8_MODEL_VERSION,
            prediction__fixture__kickoff__gte=timezone.now(),
        ).values_list("prediction_id", flat=True)
    )
    cleaned_near = []
    for row in context.get("near_premium", []):
        prediction = row["prediction"]
        if prediction.id in selected_prediction_ids:
            continue
        row = dict(row)
        row["reason"] = _human_current_rejection_reason(prediction)
        cleaned_near.append(row)
    context["near_premium"] = cleaned_near

    return render(request, "dashboard/developer.html", context)


@require_POST
def generate_premium_picks(request):
    _expire_stale_generation_jobs()
    expected_pin = os.getenv("PIPELINE_TRIGGER_PIN", "").strip()
    if not expected_pin:
        return JsonResponse({"ok": False, "message": "PIPELINE_TRIGGER_PIN no está configurado en Render."}, status=503)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "message": "Solicitud inválida."}, status=400)
    supplied_pin = str(payload.get("pin") or "").strip()
    if not hmac.compare_digest(supplied_pin, expected_pin):
        return JsonResponse({"ok": False, "message": "PIN incorrecto."}, status=403)

    target_date = _parse_target_date(payload.get("target_date"), default_today=False)
    if target_date is None:
        return JsonResponse({"ok": False, "message": "Selecciona una fecha válida para los partidos."}, status=400)
    if target_date < timezone.localdate():
        return JsonResponse({"ok": False, "message": "La generación operativa solo admite hoy o fechas futuras."}, status=400)

    active_job = PremiumGenerationJob.objects.filter(
        target_date=target_date,
        status__in=PremiumGenerationJob.ACTIVE_STATUSES,
    ).first()
    if active_job:
        return JsonResponse({"ok": True, "already_running": True, "message": "Ya hay una generación en curso para esa fecha.", "job_id": active_job.id, "target_date": active_job.target_date.isoformat()})
    now_ts = time.time()
    last_trigger = float(request.session.get("premium_trigger_ts", 0) or 0)
    if now_ts - last_trigger < 45:
        return JsonResponse({"ok": False, "message": "Espera unos segundos antes de volver a generar."}, status=429)
    mode = _interactive_pipeline_mode(target_date)
    job = PremiumGenerationJob.objects.create(
        target_date=target_date,
        mode=mode,
        status=PremiumGenerationJob.STATUS_QUEUED,
        current_stage="QUEUE",
        progress_pct=0,
        message=f"Solicitud creada para {target_date.isoformat()}; enviando al worker.",
        metadata={"source": "dashboard", "interactive_fast": True, "resolved_mode": mode, "selected_date": target_date.isoformat()},
    )
    result = GitHubPipelineTrigger().dispatch(target_date=target_date, mode=mode, generation_job_id=job.id)
    if not result.accepted:
        job.status = PremiumGenerationJob.STATUS_FAILED
        job.current_stage = "DISPATCH"
        job.progress_pct = 100
        job.message = result.message[:255]
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "current_stage", "progress_pct", "message", "finished_at"])
        return JsonResponse({"ok": False, "message": result.message, "job_id": job.id}, status=502)
    job.status = PremiumGenerationJob.STATUS_DISPATCHED
    job.current_stage = "QUEUE"
    job.progress_pct = 1
    job.message = f"En cola para {target_date.isoformat()}; modo {mode}, esperando worker de GitHub Actions."
    job.dispatched_at = timezone.now()
    job.save(update_fields=["status", "current_stage", "progress_pct", "message", "dispatched_at"])
    request.session["premium_trigger_ts"] = now_ts
    return JsonResponse({"ok": True, "message": result.message, "job_id": job.id, "target_date": target_date.isoformat(), "mode": mode})


@require_GET
def premium_generation_status(request):
    _expire_stale_generation_jobs()
    raw_job_id = request.GET.get("job_id")
    job = None
    if raw_job_id:
        try:
            job = PremiumGenerationJob.objects.select_related("pipeline").filter(pk=int(raw_job_id)).first()
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "message": "job_id inválido."}, status=400)
    if job is None:
        job = PremiumGenerationJob.objects.select_related("pipeline").first()
    if job is None:
        return JsonResponse({"ok": True, "job": None, "premium_count": 0, "stages": []})
    stages = []
    if job.pipeline_id:
        stages = [{"name": stage.name, "status": stage.status, "message": stage.message, "duration_seconds": stage.duration_seconds, "records_processed": stage.records_processed} for stage in job.pipeline.stages.all()]
    premium_count = DailyPremiumSelection.objects.filter(
        target_date=job.target_date,
        model_version=V8_MODEL_VERSION,
        prediction__market_odds__gte=PREMIUM_VALUE_MIN_ODDS,
        prediction__market_odds__lte=PREMIUM_VALUE_MAX_ODDS,
        prediction__expected_value__gte=PREMIUM_MIN_EV,
    ).count()
    return JsonResponse({
        "ok": True,
        "job": {"id": job.id, "status": job.status, "target_date": job.target_date.isoformat(), "current_stage": job.current_stage, "progress_pct": job.progress_pct, "message": job.message, "pipeline_id": job.pipeline_id, "finished": job.finished_at is not None, "mode": job.mode},
        "premium_count": premium_count,
        "stages": stages,
    })
