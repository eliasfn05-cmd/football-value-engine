from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from django.core.management.base import BaseCommand

from engine.btts_v25_policy import anti_zero_metrics
from engine.btts_v291_policy import anti_zero_decision_v291, tier_a_decision_v291
from engine.btts_v299_policy import anti_zero_decision_v299, tier_a_decision_v299
from engine.models import Fixture, Prediction

TARGETS = [
    ("America de Cali", "Alianza"),
    ("Leones del Norte", "Orense"),
    ("Recoleta", "Cerro Porteno"),
    ("Sol de America", "General Caballero JLM"),
    ("Estudiantes Merida", "Universidad Central"),
    ("Nautico", "Botafogo SP"),
    ("Gremio", "Internacional"),
    ("Inter Palmira", "Real Cundinamarca"),
    ("Oriental", "Racing Montevideo"),
    ("Metropolitanos", "Trujillanos"),
    ("Puerto Cabello", "La Guaira"),
    ("Libertad", "Emelec"),
    ("Santo Domingo", "Cumbaya"),
    ("Pereira", "Ind Medellin"),
    ("Tepatitlan", "Dorados"),
]


def norm(v):
    v = unicodedata.normalize("NFKD", v or "").encode("ascii", "ignore").decode("ascii").lower()
    v = re.sub(r"\b(fc|cf|sc|cd|club|deportivo|independiente|academia|universidad)\b", " ", v)
    return re.sub(r"[^a-z0-9]+", " ", v).strip()


def sim(a, b):
    na, nb = norm(a), norm(b)
    if na in nb or nb in na:
        return 0.95
    return SequenceMatcher(None, na, nb).ratio()


def code(decision):
    return getattr(decision, "code", "PASS") if decision is not None else "PASS"


class Command(BaseCommand):
    help = "Filter the user's Sep 3 2026 BTTS shortlist through real V2.9.1 + V2.9.9 gates."

    def handle(self, *args, **opts):
        fixtures = list(Fixture.objects.filter(kickoff__date="2026-09-03").select_related("home_team", "away_team"))
        rows = []
        self.stdout.write("BTTS SYSTEM FILTER | 2026-09-03 | V2.9.1 + V2.9.9")
        for wanted_home, wanted_away in TARGETS:
            scored = []
            for f in fixtures:
                s = (sim(wanted_home, f.home_team.name) + sim(wanted_away, f.away_team.name)) / 2
                scored.append((s, f))
            scored.sort(key=lambda x: x[0], reverse=True)
            match_score, fixture = scored[0] if scored else (0, None)
            if fixture is None or match_score < .70:
                self.stdout.write(f"MISSING | {wanted_home} vs {wanted_away} | best={match_score:.2f}")
                continue
            pred = Prediction.objects.filter(fixture=fixture, market__iexact="BTTS").order_by("-created_at").first()
            if pred is None:
                self.stdout.write(f"NO_PREDICTION | {fixture.home_team.name} vs {fixture.away_team.name} | match={match_score:.2f}")
                continue
            d291a = tier_a_decision_v291(pred)
            d299a = tier_a_decision_v299(pred)
            d299b = anti_zero_decision_v299(pred)
            m = anti_zero_metrics(pred)
            tier = "A" if d291a is None and d299a is None else ("B" if d299b is None else "BLOCK")
            weak = float(m.get("weakest_score_probability", 0) or 0)
            cal = float(m.get("calibrated_probability", 0) or 0)
            cons = float(m.get("consensus_probability", 0) or 0)
            emp = float(m.get("empirical_btts", 0) or 0)
            homeo, awayo = m.get("home_overall") or {}, m.get("away_overall") or {}
            rows.append((tier, cons, cal, weak, float(getattr(pred, "score", 0) or 0), fixture, pred, d291a, d299a, d299b, m))
            self.stdout.write(
                f"{tier} | {fixture.home_team.name} vs {fixture.away_team.name} | raw={float(getattr(pred,'score',0) or 0):.1f} "
                f"weak={weak:.3f} cal={cal:.3f} cons={cons:.3f} emp={emp:.3f} "
                f"L5score={homeo.get('last5_scored','?')}/{awayo.get('last5_scored','?')} "
                f"v291A={code(d291a)} v299A={code(d299a)} v299B={code(d299b)}"
            )
        order = {"A": 2, "B": 1, "BLOCK": 0}
        rows.sort(key=lambda r: (order[r[0]], r[1], r[2], r[3], r[4]), reverse=True)
        self.stdout.write("\nRANKING")
        for i, r in enumerate(rows, 1):
            tier, cons, cal, weak, raw, fixture, pred, d291a, d299a, d299b, m = r
            self.stdout.write(f"#{i} {tier} | {fixture.home_team.name} vs {fixture.away_team.name} | cons={cons:.3f} cal={cal:.3f} weak={weak:.3f} raw={raw:.1f}")
