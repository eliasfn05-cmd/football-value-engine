import hmac
import json
import os
import time

from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from engine.models import DailyPremiumSelection
from scanner.models import PipelineRun

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
    running = PipelineRun.objects.filter(target_date=target_date, status=PipelineRun.STATUS_RUNNING).first()
    if running:
        return JsonResponse({
            "ok": True,
            "already_running": True,
            "message": "Ya hay una generación en curso.",
            "baseline_run_id": running.id,
        })

    now_ts = time.time()
    last_trigger = float(request.session.get("premium_trigger_ts", 0) or 0)
    if now_ts - last_trigger < 45:
        return JsonResponse({"ok": False, "message": "Espera unos segundos antes de volver a generar."}, status=429)

    latest = PipelineRun.objects.first()
    baseline_run_id = latest.id if latest else 0
    result = GitHubPipelineTrigger().dispatch(target_date=target_date, mode="full")
    if not result.accepted:
        return JsonResponse({"ok": False, "message": result.message}, status=502)

    request.session["premium_trigger_ts"] = now_ts
    return JsonResponse({
        "ok": True,
        "message": result.message,
        "baseline_run_id": baseline_run_id,
        "target_date": target_date.isoformat(),
    })


@require_GET
def premium_generation_status(request):
    latest = PipelineRun.objects.first()
    if latest is None:
        return JsonResponse({"ok": True, "run": None, "premium_count": 0})

    premium_count = DailyPremiumSelection.objects.filter(
        target_date=latest.target_date,
        model_version=(latest.metadata or {}).get("model_version", "v8.0-sprint4-score"),
        prediction__fixture__kickoff__gte=timezone.now(),
    ).count()
    return JsonResponse({
        "ok": True,
        "run": {
            "id": latest.id,
            "status": latest.status,
            "target_date": latest.target_date.isoformat(),
            "duration_seconds": latest.duration_seconds,
            "finished": latest.finished_at is not None,
        },
        "premium_count": premium_count,
    })
