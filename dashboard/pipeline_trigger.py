from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class TriggerResult:
    accepted: bool
    message: str


class GitHubPipelineTrigger:
    """Dispatch the validated daily-scheduler workflow from the web dashboard.

    Secrets stay server-side in Render environment variables. The dashboard only
    sends an operator PIN; it never receives the GitHub token.
    """

    API_URL = (
        "https://api.github.com/repos/eliasfn05-cmd/football-value-engine/"
        "actions/workflows/daily-scheduler.yml/dispatches"
    )

    def __init__(self):
        self.token = os.getenv("GITHUB_ACTIONS_TOKEN", "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def dispatch(self, *, target_date: date, mode: str = "full", generation_job_id: int | None = None) -> TriggerResult:
        if not self.token:
            return TriggerResult(False, "GITHUB_ACTIONS_TOKEN no está configurado en Render.")

        inputs = {
            "mode": mode,
            "target_date": target_date.isoformat(),
        }
        if generation_job_id is not None:
            inputs["generation_job_id"] = str(int(generation_job_id))

        payload = json.dumps({"ref": "main", "inputs": inputs}).encode("utf-8")
        request = urllib.request.Request(
            self.API_URL,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "football-value-engine-dashboard",
            },
        )
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
