import unicodedata
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from backtesting.models import PredictionOutcome
from engine.models import Fixture, Prediction, PremiumPublicationLedger

RESULTS = [
    ("Portland Timbers", "Austin FC", 1, 2, "WIN"),
    ("San Diego FC", "LA Galaxy", 3, 1, "WIN"),
    ("De Graafschap", "Almere City", 1, 4, "WIN"),
    ("Servette", "Luzern", 1, 1, "WIN"),
    ("Tottenham", "Newcastle", 0, 2, "LOSS"),
]

ALIASES = {
    "portland timbers": ["portland timbers"],
    "austin fc": ["austin fc", "austin"],
    "san diego fc": ["san diego fc", "san diego"],
    "la galaxy": ["la galaxy", "los angeles galaxy"],
    "de graafschap": ["de graafschap"],
    "almere city": ["almere city", "almere city fc"],
    "servette": ["servette", "servette fc"],
    "luzern": ["luzern", "lucerna", "fc luzern"],
    "tottenham": ["tottenham", "tottenham hotspur"],
    "newcastle": ["newcastle", "newcastle united"],
}


def norm(value):
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return " ".join(value.casefold().replace("-", " ").split())


def names_for(name):
    return [norm(x) for x in ALIASES.get(norm(name), [name])]


class Command(BaseCommand):
    help = "Register five user-verified BTTS results without manufacturing Premium ledgers."

    def handle(self, *args, **opts):
        for home, away, hg, ag, result in RESULTS:
            home_names, away_names = names_for(home), names_for(away)
            fixtures = Fixture.objects.filter(home_goals__isnull=False, away_goals__isnull=False).select_related("home_team", "away_team").order_by("-kickoff")[:10000]
            matches = [f for f in fixtures if norm(f.home_team.name) in home_names and norm(f.away_team.name) in away_names]
            if not matches:
                self.stdout.write(self.style.WARNING(f"NOT FOUND | {home} vs {away} | expected {hg}-{ag} {result}"))
                continue
            fixture = matches[0]
            fixture.home_goals, fixture.away_goals = hg, ag
            fixture.save(update_fields=["home_goals", "away_goals"])

            preds = Prediction.objects.filter(fixture=fixture, market__iexact="BTTS").order_by("-created_at")
            pred = preds.first()
            if pred is None:
                self.stdout.write(self.style.WARNING(f"FIXTURE ONLY | fixture={fixture.id} | {home} vs {away} {hg}-{ag} {result} | no BTTS prediction"))
                continue

            ledger = PremiumPublicationLedger.objects.filter(prediction=pred).first()
            odds = Decimal(str(pred.market_odds)) if pred.market_odds is not None else None
            profit = (odds - Decimal("1")) if result == "WIN" and odds else (Decimal("-1") if result == "LOSS" else Decimal("0"))
            outcome, _ = PredictionOutcome.objects.update_or_create(
                prediction=pred,
                defaults={
                    "result": result,
                    "home_goals": hg,
                    "away_goals": ag,
                    "stake_units": Decimal("1"),
                    "profit_units": profit,
                    "settled_at": timezone.now(),
                    "settlement_reason": "manual_verified_btts_20260830",
                },
            )
            scope = "OFFICIAL_LEDGER" if ledger else "NON_LEDGER_PREDICTION"
            self.stdout.write(self.style.SUCCESS(
                f"REGISTERED | fixture={fixture.id} pred={pred.id} outcome={outcome.id} scope={scope} | {home} vs {away} {hg}-{ag} {result}"
            ))
        self.stdout.write("Done. No PremiumPublicationLedger rows were created by this command.")
