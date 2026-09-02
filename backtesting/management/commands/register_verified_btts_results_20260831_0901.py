import re
import unicodedata
from difflib import SequenceMatcher

from django.core.management.base import BaseCommand
from django.db import transaction

from engine.models import Fixture, Prediction

VERIFIED = [
    ("2026-08-31", "Remo", "Coritiba", 2, 3, "WIN", "A#1"),
    ("2026-08-31", "U. Catolica", "O'Higgins", 3, 2, "WIN", "A#2"),
    ("2026-08-31", "Cobreloa", "San Marcos de Arica", 2, 0, "LOSS", "A#3"),
    ("2026-09-01", "Helsingborg", "Orebro", 1, 1, "WIN", "A#1"),
    ("2026-09-01", "Xamax", "Yverdon", 1, 2, "WIN", "A#2"),
    ("2026-09-01", "Rapperswil-Jona", "Kriens", 1, 1, "WIN", "A#3"),
    ("2026-09-01", "Grau", "Melgar", 1, 0, "LOSS", "A#1"),
    ("2026-09-01", "Universitario de Vinto", "Aurora", 1, 1, "WIN", "A#2"),
    ("2026-09-01", "Barranquilla", "Boca Juniors", 2, 1, "WIN", "A#3"),
]


def norm(value):
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii").lower()
    value = re.sub(r"\b(fc|cf|if|cd|club|deportes|u|universidad)\b", " ", value)
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def sim(a, b):
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


class Command(BaseCommand):
    help = "Register user-verified BTTS top-3 results for 2026-08-31 and 2026-09-01."

    @transaction.atomic
    def handle(self, *args, **options):
        self.stdout.write("VERIFIED BTTS TOP-3 RESULTS | 2026-08-31 / 2026-09-01")
        found = updated = missing = wins = losses = 0
        for day, home, away, hg, ag, state, rank in VERIFIED:
            candidates = list(Fixture.objects.filter(kickoff__date=day).select_related("home_team", "away_team"))
            scored = []
            for fixture in candidates:
                hs = sim(home, fixture.home_team.name)
                as_ = sim(away, fixture.away_team.name)
                scored.append(((hs + as_) / 2.0, fixture))
            scored.sort(key=lambda x: x[0], reverse=True)
            best_score, fixture = scored[0] if scored else (0.0, None)
            if fixture is None or best_score < 0.72:
                missing += 1
                best = "none" if fixture is None else f"{fixture.home_team.name} vs {fixture.away_team.name} ({best_score:.2f})"
                self.stdout.write(f"MISSING | {rank} | {day} | {home} vs {away} | {hg}-{ag} | {state} | best={best}")
                continue
            found += 1
            wins += int(state == "WIN")
            losses += int(state == "LOSS")
            changed = fixture.home_goals != hg or fixture.away_goals != ag
            if changed:
                fixture.home_goals = hg
                fixture.away_goals = ag
                fixture.save(update_fields=["home_goals", "away_goals"])
                updated += 1
            has_prediction = Prediction.objects.filter(fixture=fixture, market__iexact="BTTS").exists()
            source = "PREDICTION" if has_prediction else "NO_BTTS_PREDICTION"
            loss_type = "ONE_SIDED" if state == "LOSS" and ((hg == 0) ^ (ag == 0)) else ("ZERO_ZERO" if hg == 0 and ag == 0 else "-")
            self.stdout.write(
                f"{'UPDATED' if changed else 'OK'} | {rank} | {day} | {fixture.home_team.name} vs {fixture.away_team.name} | "
                f"{hg}-{ag} | {state} | {loss_type} | {source} | match={best_score:.2f}"
            )
        total = wins + losses
        hit = wins / total if total else 0.0
        self.stdout.write(self.style.SUCCESS(
            f"SUMMARY | supplied={len(VERIFIED)} found={found} updated={updated} missing={missing} "
            f"wins={wins} losses={losses} hit={hit:.4f}. Scores are user-verified; no Prediction/PremiumPublicationLedger rows are fabricated."
        ))
