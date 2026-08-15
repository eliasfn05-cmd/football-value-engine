from __future__ import annotations

import hmac
import json
import os

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST

from scanner.models import PremiumGenerationJob

from .pipeline_trigger import GitHubPipelineTrigger


@require_POST
def cancel_premium_generation(request):
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

    try:
        job_id = int(payload.get("job_id"))
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "message": "job_id inválido."}, status=400)

    job = PremiumGenerationJob.objects.filter(pk=job_id).first()
    if job is None:
        return JsonResponse({"ok": False, "message": "La generación ya no existe."}, status=404)

    if job.status not in PremiumGenerationJob.ACTIVE_STATUSES:
        return JsonResponse({"ok": True, "already_finished": True, "message": "La generación ya había terminado."})

    trigger = GitHubPipelineTrigger()
    result = trigger.cancel_generation(job)
    if not result.accepted:
        return JsonResponse({"ok": False, "message": result.message}, status=502)

    now = timezone.now()
    metadata = dict(job.metadata or {})
    if result.run_id:
        metadata["github_run_id"] = result.run_id
    metadata["cancelled_by"] = "dashboard"
    metadata["cancelled_at"] = now.isoformat()
    job.status = PremiumGenerationJob.STATUS_FAILED
    job.current_stage = "CANCELLED"
    job.progress_pct = 100
    job.message = "Generación cancelada manualmente. El proceso de GitHub Actions fue detenido."
    job.finished_at = now
    job.metadata = metadata
    job.save(update_fields=["status", "current_stage", "progress_pct", "message", "finished_at", "metadata"])

    return JsonResponse({"ok": True, "message": job.message, "job_id": job.id, "run_id": result.run_id})
