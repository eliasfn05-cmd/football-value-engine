"""Filtro duro de riesgo de 0-0 / <=1 gol para candidatos Over 1.5.

Fail closed: datos incompletos o probabilidades sin calibrar nunca aprueban.
No promete eliminar los 0-0: reduce la exposición a perfiles de riesgo.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import exp, isfinite
from typing import Sequence


@dataclass(frozen=True)
class TeamEvidence:
    overall_last10: Sequence[tuple[int, int]]
    role_last10: Sequence[tuple[int, int]]
    # Marcadores (goles a favor, goles en contra), del más reciente al más antiguo.
    # role = local para el anfitrión, visitante para el rival.


@dataclass(frozen=True)
class Decision:
    approved: bool
    tier: str
    reasons: tuple[str, ...]
    probability_over15: float | None


def poisson_over15(total_lambda: float) -> float:
    if not isfinite(total_lambda) or total_lambda < 0:
        raise ValueError("lambda debe ser finito y no negativo")
    return 1.0 - exp(-total_lambda) * (1.0 + total_lambda)


def _validate_scores(scores: Sequence[tuple[int, int]], label: str, reasons: list[str]) -> bool:
    if len(scores) < 10:
        reasons.append(f"{label}: faltan partidos (requiere 10)")
        return False
    if any(not isinstance(a, int) or not isinstance(b, int) or a < 0 or b < 0
           for a, b in scores[:10]):
        reasons.append(f"{label}: marcador inválido")
        return False
    return True


def assess_over15(
    home: TeamEvidence,
    away: TeamEvidence,
    *,
    total_lambda: float | None,
    calibrated: bool,
    h2h_recent: Sequence[tuple[int, int]] | None = None,
) -> Decision:
    reasons: list[str] = []
    for label, team in (("local", home), ("visitante", away)):
        for segment, scores in (("global", team.overall_last10), ("rol", team.role_last10)):
            key = f"{label}/{segment}"
            if not _validate_scores(scores, key, reasons):
                continue
            recent = scores[:5]
            if any(a + b <= 1 for a, b in recent):
                reasons.append(f"{key}: resultado <=1 gol en últimos 5")
            if sum(a + b >= 2 for a, b in scores[:10]) < 8:
                reasons.append(f"{key}: menos de 8/10 Over 1.5")
            if sum(a == 0 for a, _ in scores[:10]) > 2:
                reasons.append(f"{key}: más de 2 partidos sin marcar en 10")
            if sum(a for a, _ in recent) / 5 < (1.25 if segment == "rol" else 1.20):
                reasons.append(f"{key}: promedio ofensivo insuficiente")
            if sum(a + b for a, b in recent) / 5 < (2.80 if segment == "rol" else 2.70):
                reasons.append(f"{key}: promedio total insuficiente")
    if h2h_recent is None:
        reasons.append("H2H: datos ausentes")
    elif h2h_recent:
        if any(not isinstance(a, int) or not isinstance(b, int) or a < 0 or b < 0
               for a, b in h2h_recent):
            reasons.append("H2H: datos inválidos")
        else:
            if sum(h2h_recent[0]) <= 1:
                reasons.append("H2H: último enfrentamiento <=1 gol")
            if len(h2h_recent) >= 5 and sum(a + b <= 1 for a, b in h2h_recent[:5]) >= 2:
                reasons.append("H2H: al menos 2/5 con <=1 gol")
    else:
        reasons.append("H2H: sin antecedentes; requiere evaluación manual")

    probability = None
    if total_lambda is None or not calibrated:
        reasons.append("Poisson: lambda ausente o modelo no calibrado")
    else:
        try:
            probability = poisson_over15(total_lambda)
            if total_lambda < 3.05:
                reasons.append("Poisson: lambda total <3.05")
            if probability < 0.81:
                reasons.append("Poisson: P(Over 1.5) <81%")
        except (ValueError, TypeError):
            reasons.append("Poisson: lambda inválido")
    if reasons:
        return Decision(False, "DESCARTAR", tuple(reasons), probability)
    return Decision(True, "CANDIDATO_APTO_NO_EV", (), probability)
