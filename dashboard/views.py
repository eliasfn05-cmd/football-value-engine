import hmac
import json
import os
import time

from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from engine.models import DailyPremiumSelection
from scanner.models import PremiumGenerationJob

from .pipeline_trigger import GitHubPipelineTrigger
from .services import DashboardService


def health(request):
    return JsonResponse({
        "status": "ok",
        "service": "football-value-engine",
        "version": "1.0.0",
    })


def dashboard_home(request):
    context = DashboardService().build()
    context["pipeline_trigger_configured"] = GitHubPipelineTrigger().configured and bool(
        os.getenv("PIPELINE_TRIGGER_PIN", "").strip()
    )
    active_job = PremiumGenerationJob.objects.filter(
        target_date=timezone.localdate(),
        status__in=PremiumGenerationJob.ACTIVE_STATUSES,
    ).first()
    context["active_generation_job_id"] = active_job.id if active_job else None
    return render(request, "dashboard/index.html", context)


def developer_dashboard(request):
    context = DashboardService().build_developer()
    return render(request, "dashboard/developer.html", context)


@require_POST
def generate_premium_picks(request):
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

    target_date = timezone.localdate()
    active_job = PremiumGenerationJob.objects.filter(
        target_date=target_date,
        status__in=PremiumGenerationJob.ACTIVE_STATUSES,
    ).first()
    if active_job:
        return JsonResponse({
            "ok": True,
            "already_running": True,
            "message": "Ya hay una generación en curso.",
            "job_id": active_job.id,
            "target_date": active_job.target_date.isoformat(),
        })

    now_ts = time.time()
    last_trigger = float(request.session.get("premium_trigger_ts", 0) or 0)
    if now_ts - last_trigger < 45:
        return JsonResponse({"ok": False, "message": "Espera unos segundos antes de volver a generar."}, status=429)

    job = PremiumGenerationJob.objects.create(
        target_date=target_date,
        mode="full",
        status=PremiumGenerationJob.STATUS_QUEUED,
        current_stage="QUEUE",
        progress_pct=0,
        message="Solicitud creada; enviando al worker.",
        metadata={"source": "dashboard"},
    )
    result = GitHubPipelineTrigger().dispatch(
        target_date=target_date,
        mode="full",
        generation_job_id=job.id,
    )
    if not result.accepted:
        job.status = PremiumGenerationJob.STATUS_FAILED
        job.current_stage = "DISPATCH"
        job.message = result.message[:255]
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "current_stage", "message", "finished_at"])
        return JsonResponse({"ok": False, "message": result.message, "job_id": job.id}, status=502)

    job.status = PremiumGenerationJob.STATUS_DISPATCHED
    job.current_stage = "QUEUE"
    job.progress_pct = 1
    job.message = "En cola; esperando worker de GitHub Actions."
    job.dispatched_at = timezone.now()
    job.save(update_fields=["status", "current_stage", "progress_pct", "message", "dispatched_at"])
    request.session["premium_trigger_ts"] = now_ts
    return JsonResponse({
        "ok": True,
        "message": result.message,
        "job_id": job.id,
        "target_date": target_date.isoformat(),
    })


@require_GET
def premium_generation_status(request):
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
        stages = [
            {
                "name": stage.name,
                "status": stage.status,
                "message": stage.message,
                "duration_seconds": stage.duration_seconds,
                "records_processed": stage.records_processed,
            }
            for stage in job.pipeline.stages.all()
        ]

    premium_count = DailyPremiumSelection.objects.filter(
        target_date=job.target_date,
        prediction__fixture__kickoff__gte=timezone.now(),
    ).count()
    return JsonResponse({
        "ok": True,
        "job": {
            "id": job.id,
            "status": job.status,
            "target_date": job.target_date.isoformat(),
            "current_stage": job.current_stage,
            "progress_pct": job.progress_pct,
            "message": job.message,
            "pipeline_id": job.pipeline_id,
            "finished": job.finished_at is not None,
        },
        "premium_count": premium_count,
        "stages": stages,
    })
