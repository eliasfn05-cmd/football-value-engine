from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from django.core.management.base import BaseCommand

from engine.btts_v25_policy import anti_zero_metrics
from engine.btts_v27_policy import _opponent_concession_metrics
from engine.btts_v291_policy import tier_a_decision_v291
from engine.btts_v299_policy import tier_a_decision_v299
from engine.models import Fixture, Prediction

CASES = [
    ("2026-09-03", "Copenhagen", "Nordsjaelland", 2, 0),
    ("2026-09-03", "Lugano", "Servette", 1, 0),
]


def norm(value):
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii").lower()
    value = re.sub(r"\b(fc|cf|if|club)\b", " ", value)
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def sim(a, b):
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def decision_text(decision):
    if decision is None:
        return "PASS"
    code = getattr(decision, "code", getattr(decision, "reason_code", "blocked"))
    reason = getattr(decision, "reason", getattr(decision, "message", str(decision)))
    return f"BLOCK | {code} | {reason}"


class Command(BaseCommand):
    help = "Read-only V2.9.9 case audit for Sep 3 one-sided BTTS Top-3 losses."

    def handle(self, *args, **options):
        self.stdout.write("BTTS V2.9.9 ONE-SIDED CASE AUDIT | READ ONLY")
        total = v291_blocked = v299_blocked = 0
        for day, home, away, hg, ag in CASES:
            fixtures = list(Fixture.objects.filter(kickoff__date=day).select_related("home_team", "away_team"))
            ranked = sorted(
                [((sim(home, f.home_team.name) + sim(away, f.away_team.name)) / 2.0, f) for f in fixtures],
                key=lambda x: x[0], reverse=True,
            )
            match_score, fixture = ranked[0] if ranked else (0.0, None)
            if fixture is None or match_score < 0.72:
                self.stdout.write(f"MISSING | {home} vs {away}")
                continue
            prediction = (
                Prediction.objects.filter(fixture=fixture, market__iexact="BTTS")
                .order_by("-created_at", "-id").first()
            )
            if not prediction:
                self.stdout.write(f"NO_PREDICTION | {fixture.home_team.name} vs {fixture.away_team.name}")
                continue
            total += 1
            d291 = tier_a_decision_v291(prediction)
            d299 = tier_a_decision_v299(prediction)
            v291_blocked += int(d291 is not None)
            v299_blocked += int(d299 is not None)
            m = anti_zero_metrics(prediction)
            c = _opponent_concession_metrics(prediction)
            self.stdout.write(f"\nCASE | {fixture.home_team.name} vs {fixture.away_team.name} | result={hg}-{ag} | ONE_SIDED | match={match_score:.2f}")
            self.stdout.write(f"V2.9.1 | {decision_text(d291)}")
            self.stdout.write(f"V2.9.9 | {decision_text(d299)}")
            if m.get("available"):
                self.stdout.write(
                    "METRICS | weakest={:.3f} calibrated={:.3f} consensus={:.3f} homeFTS={:.3f} awayFTS={:.3f} homeL5scored={} awayL5scored={}".format(
                        float(m.get("weakest_score_probability", 0)), float(m.get("calibrated_probability", 0)),
                        float(m.get("consensus_probability", 0)), float(m["home_overall"].get("failed_to_score_rate", 0)),
                        float(m["away_overall"].get("failed_to_score_rate", 0)), int(m["home_overall"].get("last5_scored", 0)),
                        int(m["away_overall"].get("last5_scored", 0)),
                    )
                )
            if c.get("available"):
                self.stdout.write(
                    "CONCESSION | home_leg_opp_conceded={}/5 cs={} | away_leg_opp_conceded={}/5 cs={}".format(
                        c["home_scoring_vs"].get("last5_conceded"), c["home_scoring_vs"].get("last5_clean_sheets"),
                        c["away_scoring_vs"].get("last5_conceded"), c["away_scoring_vs"].get("last5_clean_sheets"),
                    )
                )
        self.stdout.write(self.style.SUCCESS(
            f"\nSUMMARY | cases={total} v291_blocked={v291_blocked} v299_blocked={v299_blocked} "
            f"v299_prevented={v299_blocked - v291_blocked}"
        ))
        self.stdout.write("DECISION | Do not promote V2.9.9 from two cases alone; use this audit to verify the intended ONE_SIDED gates fire pre-kickoff.")
