import re
import unicodedata
from difflib import SequenceMatcher

from django.core.management.base import BaseCommand
from django.db import transaction

from engine.models import Fixture, Prediction

VERIFIED = [
    ("2026-09-03", "Copenhagen", "Nordsjaelland", 2, 0, "LOSS", "TOP3"),
    ("2026-09-03", "Lugano", "Servette", 1, 0, "LOSS", "TOP3"),
    ("2026-09-03", "Sol de America", "General Caballero JLM", 1, 1, "WIN", "B+"),
    ("2026-09-03", "Recoleta", "Cerro Porteno", 1, 3, "WIN", "B"),
    ("2026-09-03", "Puerto Cabello", "La Guaira", 1, 1, "WIN", "B-"),
    ("2026-09-03", "Tepatitlan", "Dorados", 1, 4, "WIN", "B"),
]


def norm(value):
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii").lower()
    value = re.sub(r"\b(fc|cf|if|club)\b", " ", value)
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def sim(a, b):
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


class Command(BaseCommand):
    help = "Register user-verified BTTS tracked results for 2026-09-03."

    @transaction.atomic
    def handle(self, *args, **options):
        self.stdout.write("VERIFIED BTTS TRACKED RESULTS | 2026-09-03")
        found = updated = missing = wins = losses = one_sided = zero_zero = 0
        for day, home, away, hg, ag, state, rank in VERIFIED:
            candidates = list(Fixture.objects.filter(kickoff__date=day).select_related("home_team", "away_team"))
            scored = sorted(
                [((sim(home, f.home_team.name) + sim(away, f.away_team.name)) / 2.0, f) for f in candidates],
                key=lambda x: x[0], reverse=True,
            )
            best_score, fixture = scored[0] if scored else (0.0, None)
            if fixture is None or best_score < 0.72:
                missing += 1
                self.stdout.write(f"MISSING | {rank} | {home} vs {away} | {hg}-{ag}")
                continue
            found += 1
            changed = fixture.home_goals != hg or fixture.away_goals != ag
            if changed:
                fixture.home_goals, fixture.away_goals = hg, ag
                fixture.save(update_fields=["home_goals", "away_goals"])
                updated += 1
            has_prediction = Prediction.objects.filter(fixture=fixture, market__iexact="BTTS").exists()
            is_btts = hg > 0 and ag > 0
            actual_state = "WIN" if is_btts else "LOSS"
            loss_type = "-" if is_btts else ("ZERO_ZERO" if hg == 0 and ag == 0 else "ONE_SIDED")
            wins += int(is_btts)
            losses += int(not is_btts)
            one_sided += int(loss_type == "ONE_SIDED")
            zero_zero += int(loss_type == "ZERO_ZERO")
            self.stdout.write(
                f"{'UPDATED' if changed else 'OK'} | {rank} | {fixture.home_team.name} vs {fixture.away_team.name} | "
                f"{hg}-{ag} | {actual_state} | {loss_type} | {'PREDICTION' if has_prediction else 'NO_BTTS_PREDICTION'} | match={best_score:.2f}"
            )
        hit = wins / found if found else 0.0
        self.stdout.write(self.style.SUCCESS(
            f"SUMMARY | supplied={len(VERIFIED)} found={found} updated={updated} missing={missing} wins={wins} losses={losses} "
            f"hit={hit:.4f} zero_zero={zero_zero} one_sided={one_sided}. "
            "Scores are user-verified; no Prediction/PremiumPublicationLedger rows are fabricated."
        ))
