from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class TriggerResult:
    accepted: bool
    message: str
    run_id: int | None = None


class GitHubPipelineTrigger:
    """Dispatch and cancel the validated daily-scheduler workflow from the dashboard."""

    REPO_API = "https://api.github.com/repos/eliasfn05-cmd/football-value-engine"
    API_URL = f"{REPO_API}/actions/workflows/daily-scheduler.yml/dispatches"
    RUNS_URL = f"{REPO_API}/actions/workflows/daily-scheduler.yml/runs"

    def __init__(self):
        self.token = os.getenv("GITHUB_ACTIONS_TOKEN", "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def _headers(self, *, json_content: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "football-value-engine-dashboard",
        }
        if json_content:
            headers["Content-Type"] = "application/json"
        return headers

    def dispatch(
        self,
        *,
        target_date: date,
        mode: str = "full",
        generation_job_id: int | None = None,
        start_time: str = "00:00",
        end_time: str = "23:59",
    ) -> TriggerResult:
        if not self.token:
            return TriggerResult(False, "GITHUB_ACTIONS_TOKEN no está configurado en Render.")

        inputs = {
            "mode": mode,
            "target_date": target_date.isoformat(),
            "start_time": start_time,
            "end_time": end_time,
        }
        if generation_job_id is not None:
            inputs["generation_job_id"] = str(int(generation_job_id))

        payload = json.dumps({"ref": "main", "inputs": inputs}).encode("utf-8")
        request = urllib.request.Request(self.API_URL, data=payload, method="POST", headers=self._headers(json_content=True))
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                status = int(getattr(response, "status", 0) or 0)
            if status == 204:
                return TriggerResult(True, "Generación de Picks Premium enviada correctamente.")
            return TriggerResult(False, f"GitHub respondió con estado inesperado {status}.")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            return TriggerResult(False, f"GitHub rechazó la solicitud ({exc.code}). {detail}".strip())
        except urllib.error.URLError as exc:
            return TriggerResult(False, f"No se pudo contactar GitHub Actions: {exc.reason}")
        except TimeoutError:
            return TriggerResult(False, "GitHub Actions no respondió a tiempo.")

    def _active_run_id_for_job(self, job) -> int | None:
        metadata = dict(job.metadata or {})
        stored = metadata.get("github_run_id")
        if stored:
            try:
                return int(stored)
            except (TypeError, ValueError):
                pass

        params = urllib.parse.urlencode({"event": "workflow_dispatch", "per_page": 30})
        request = urllib.request.Request(f"{self.RUNS_URL}?{params}", headers=self._headers())
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))

        lower_bound = (job.dispatched_at or job.requested_at) - timedelta(minutes=5)
        candidates = []
        for run in payload.get("workflow_runs", []):
            if run.get("status") not in {"queued", "in_progress", "waiting", "pending", "requested"}:
                continue
            created_raw = run.get("created_at")
            if not created_raw:
                continue
            try:
                from datetime import datetime
                created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if created < lower_bound:
                continue
            candidates.append((created, int(run["id"])))

        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][1]

    def cancel_generation(self, job) -> TriggerResult:
        if not self.token:
            return TriggerResult(False, "GITHUB_ACTIONS_TOKEN no está configurado en Render.")
        try:
            run_id = self._active_run_id_for_job(job)
            if run_id is None:
                return TriggerResult(False, "No se encontró una ejecución activa de GitHub Actions para este trabajo.")
            request = urllib.request.Request(
                f"{self.REPO_API}/actions/runs/{run_id}/cancel",
                data=b"{}",
                method="POST",
                headers=self._headers(json_content=True),
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                status = int(getattr(response, "status", 0) or 0)
            if status == 202:
                return TriggerResult(True, "Cancelación enviada a GitHub Actions.", run_id=run_id)
            return TriggerResult(False, f"GitHub respondió con estado inesperado {status} al cancelar.", run_id=run_id)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            return TriggerResult(False, f"GitHub rechazó la cancelación ({exc.code}). {detail}".strip())
        except urllib.error.URLError as exc:
            return TriggerResult(False, f"No se pudo contactar GitHub Actions para cancelar: {exc.reason}")
        except TimeoutError:
            return TriggerResult(False, "GitHub Actions no respondió a tiempo al cancelar.")
