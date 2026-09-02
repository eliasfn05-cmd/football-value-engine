from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

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


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower().replace("ø", "o").replace("ł", "l")
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _sim(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.95
    return SequenceMatcher(None, na, nb).ratio()


def _find_fixture(day: str, home: str, away: str):
    fixtures = list(
        Fixture.objects.select_related("home_team", "away_team")
        .filter(kickoff__date=day)
        .order_by("kickoff")
    )
    if not fixtures:
        return None, 0.0

    scored = []
    for fixture in fixtures:
        hs = _sim(home, fixture.home_team.name)
        aw = _sim(away, fixture.away_team.name)
        pair = (hs + aw) / 2.0
        scored.append((pair, min(hs, aw), fixture))

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    pair, weakest, fixture = scored[0]
    if pair >= 0.72 and weakest >= 0.55:
        return fixture, pair
    return None, pair


class Command(BaseCommand):
    help = (
        "Register user-verified BTTS top-3 results for 2026-08-30/31. "
        "Uses tolerant same-day team matching and never fabricates predictions/ledger rows."
    )

    @transaction.atomic
    def handle(self, *args, **options):
        self.stdout.write("VERIFIED BTTS TOP-3 RESULTS | 2026-08-30/31")
        found = updated = missing = 0

        for day, home, away, hg, ag, state, rank in VERIFIED:
            fixture, match_score = _find_fixture(day, home, away)
            if fixture is None:
                missing += 1
                self.stdout.write(
                    f"MISSING | {rank} | {day} | {home} vs {away} | {hg}-{ag} | {state} | best_match={match_score:.2f}"
                )
                continue

            found += 1
            changed = fixture.home_goals != hg or fixture.away_goals != ag
            if changed:
                fixture.home_goals = hg
                fixture.away_goals = ag
                fixture.save(update_fields=["home_goals", "away_goals"])
                updated += 1

            has_btts_prediction = Prediction.objects.filter(
                fixture=fixture,
                market__iexact="BTTS",
            ).exists()
            source = "PREDICTION" if has_btts_prediction else "NO_BTTS_PREDICTION"
            self.stdout.write(
                f"{'UPDATED' if changed else 'OK'} | {rank} | {day} | "
                f"{fixture.home_team.name} vs {fixture.away_team.name} | {hg}-{ag} | {state} | "
                f"{source} | match={match_score:.2f}"
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"SUMMARY | supplied={len(VERIFIED)} found={found} updated={updated} missing={missing}. "
                "Scores are user-verified; no Prediction/PremiumPublicationLedger rows are fabricated."
            )
        )
