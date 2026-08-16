from __future__ import annotations

import hmac
import json
import os
import time

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST

from engine.time_window import normalize_clock, window_bounds
from scanner.models import PremiumGenerationJob

from .pipeline_trigger import GitHubPipelineTrigger
from .views import _expire_stale_generation_jobs, _parse_target_date


@require_POST
def generate_premium_picks_windowed(request):
    """Create an interactive Premium job scoped only by kickoff time.

    This endpoint changes execution volume only. Model probabilities, scoring,
    calibration, Deep Analysis, risk guards and Premium gates are untouched.
    """
    _expire_stale_generation_jobs()
    expected_pin = os.getenv("PIPELINE_TRIGGER_PIN", "").strip()
    if not expected_pin:
        return JsonResponse(
            {"ok": False, "message": "PIPELINE_TRIGGER_PIN no está configurado en Render."},
            status=503,
        )

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "message": "Solicitud inválida."}, status=400)

    supplied_pin = str(payload.get("pin") or "").strip()
    if not hmac.compare_digest(supplied_pin, expected_pin):
        return JsonResponse({"ok": False, "message": "PIN incorrecto."}, status=403)

    target_date = _parse_target_date(payload.get("target_date"), default_today=False)
    if target_date is None:
        return JsonResponse(
            {"ok": False, "message": "Selecciona una fecha válida para los partidos."},
            status=400,
        )
    if target_date < timezone.localdate():
        return JsonResponse(
            {"ok": False, "message": "La generación operativa solo admite hoy o fechas futuras."},
            status=400,
        )

    try:
        start_time = normalize_clock(payload.get("start_time"), default="00:00")
        end_time = normalize_clock(payload.get("end_time"), default="23:59")
        window_bounds(target_date, start_time=start_time, end_time=end_time)
    except ValueError:
        return JsonResponse(
            {"ok": False, "message": "El rango horario es inválido. Usa HH:MM y asegúrate de que Hora fin sea igual o posterior a Hora inicio."},
            status=400,
        )

    active_job = PremiumGenerationJob.objects.filter(
        target_date=target_date,
        status__in=PremiumGenerationJob.ACTIVE_STATUSES,
    ).first()
    if active_job:
        return JsonResponse(
            {
                "ok": True,
                "already_running": True,
                "message": "Ya hay una generación en curso para esa fecha.",
                "job_id": active_job.id,
                "target_date": active_job.target_date.isoformat(),
            }
        )

    now_ts = time.time()
    last_trigger = float(request.session.get("premium_trigger_ts", 0) or 0)
    if now_ts - last_trigger < 45:
        return JsonResponse(
            {"ok": False, "message": "Espera unos segundos antes de volver a generar."},
            status=429,
        )

    # Interactive range jobs always use the selective refresh pipeline. The
    # workflow performs cheap fixture ingest + block-only V8 bootstrap first.
    mode = "refresh"
    label = f"{start_time}-{end_time}"
    job = PremiumGenerationJob.objects.create(
        target_date=target_date,
        mode=mode,
        status=PremiumGenerationJob.STATUS_QUEUED,
        current_stage="QUEUE",
        progress_pct=0,
        message=f"Solicitud creada para {target_date.isoformat()} · bloque {label}; enviando al worker.",
        metadata={
            "source": "dashboard",
            "interactive_fast": True,
            "resolved_mode": mode,
            "selected_date": target_date.isoformat(),
            "start_time": start_time,
            "end_time": end_time,
            "kickoff_window": label,
        },
    )
    result = GitHubPipelineTrigger().dispatch(
        target_date=target_date,
        mode=mode,
        generation_job_id=job.id,
        start_time=start_time,
        end_time=end_time,
    )
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
    job.message = f"En cola para {target_date.isoformat()} · {label}; esperando worker de GitHub Actions."
    job.dispatched_at = timezone.now()
    job.save(update_fields=["status", "current_stage", "progress_pct", "message", "dispatched_at"])
    request.session["premium_trigger_ts"] = now_ts
    return JsonResponse(
        {
            "ok": True,
            "message": result.message,
            "job_id": job.id,
            "target_date": target_date.isoformat(),
            "mode": mode,
            "start_time": start_time,
            "end_time": end_time,
        }
    )
