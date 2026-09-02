from django.core.management.base import BaseCommand
from django.db import transaction

from engine.models import Fixture, Prediction


VERIFIED = [
    ("2026-08-30", "Nordsjaelland", "Brondby", 3, 1, "WIN", "A#1"),
    ("2026-08-30", "St. Gallen", "Thun", 3, 4, "WIN", "A#2"),
    ("2026-08-30", "Chelsea", "Brighton", 4, 3, "WIN", "A#3"),
    ("2026-08-30", "Columbus Crew", "New England Revolution", 1, 3, "WIN", "A#1"),
    ("2026-08-30", "St. Louis City", "FC Dallas", 3, 3, "WIN", "A#2"),
    ("2026-08-30", "Widzew Lodz", "Lech Poznan", 2, 3, "WIN", "A#3"),
    ("2026-08-31", "Sirius", "Malmo", 0, 1, "LOSS", "A#1"),
    ("2026-08-31", "GAIS", "Brommapojkarna", 4, 0, "LOSS", "A#2"),
    ("2026-08-31", "Gnistan", "TPS Turku", 1, 1, "WIN", "A#3"),
]


class Command(BaseCommand):
    help = "Register user-verified BTTS top-3 results for 2026-08-30/31 in fixture history when matching fixtures exist."

    @transaction.atomic
    def handle(self, *args, **options):
        self.stdout.write("VERIFIED BTTS TOP-3 RESULTS | 2026-08-30/31")
        found = updated = missing = 0
        for day, home, away, hg, ag, state, rank in VERIFIED:
            qs = Fixture.objects.filter(
                kickoff__date=day,
                home_team__name__iexact=home,
                away_team__name__iexact=away,
            ).order_by("-kickoff")
            fixture = qs.first()
            if fixture is None:
                missing += 1
                self.stdout.write(f"MISSING | {rank} | {day} | {home} vs {away} | {hg}-{ag} | {state}")
                continue
            found += 1
            changed = fixture.home_score != hg or fixture.away_score != ag
            if changed:
                fixture.home_score = hg
                fixture.away_score = ag
                fixture.save(update_fields=["home_score", "away_score"])
                updated += 1
            has_btts_prediction = Prediction.objects.filter(fixture=fixture, market__iexact="BTTS").exists()
            source = "PREDICTION" if has_btts_prediction else "NO_BTTS_PREDICTION"
            self.stdout.write(
                f"{'UPDATED' if changed else 'OK'} | {rank} | {day} | {home} vs {away} | {hg}-{ag} | {state} | {source}"
            )
        self.stdout.write(self.style.SUCCESS(
            f"SUMMARY | supplied={len(VERIFIED)} found={found} updated={updated} missing={missing}. "
            "Scores are user-verified; command does not fabricate Prediction/Premium ledger rows."
        ))
