import unicodedata

from django.core.management.base import BaseCommand

from engine.models import Fixture, Prediction, PremiumPublicationLedger
from backtesting.models import PredictionOutcome


VERIFIED = [
    ("Polessya", "Zorya Luhansk", 1, 2, "WIN"),
    ("Al Anwar", "Al-Ahli Jeddah", 1, 2, "WIN"),
    ("Stockholm Internationale", "Jarfalla", 2, 0, "LOSS"),
    ("Audax Italiano", "U. La Calera", 0, 0, "LOSS"),
    ("Deportivo Cuenca", "Mushuc Runa", 0, 0, "LOSS"),
    ("Leon", "Monterrey", 2, 0, "LOSS"),
    ("RKC Waalwijk", "Jong PSV", 2, 2, "WIN"),
    ("Winterthur", "Lausanne Ouchy", 1, 1, "WIN"),
]


def norm(v):
    v = unicodedata.normalize("NFKD", str(v or ""))
    return "".join(c for c in v if not unicodedata.combining(c)).lower().strip()


ALIASES = {
    norm("Jarfalla"): {norm("Jarfalla"), norm("Järfälla")},
    norm("U. La Calera"): {norm("U. La Calera"), norm("Union La Calera"), norm("Unión La Calera")},
    norm("Leon"): {norm("Leon"), norm("León")},
    norm("Lausanne Ouchy"): {norm("Lausanne Ouchy"), norm("Stade Lausanne Ouchy"), norm("Lausanne-Ouchy")},
    norm("Al-Ahli Jeddah"): {norm("Al-Ahli Jeddah"), norm("Al Ahli Jeddah"), norm("Al Ahli")},
}


def match(actual, expected):
    return norm(actual) in ALIASES.get(norm(expected), {norm(expected)})


class Command(BaseCommand):
    help = "Audit fixture/prediction/ledger/outcome identity for user-verified BTTS results. Read-only."

    def handle(self, *args, **options):
        self.stdout.write("BTTS VERIFIED FIXTURE IDENTITY AUDIT | READ-ONLY")
        self.stdout.write("Expected score is user-verified 90-minute result. No rows are modified.\n")
        all_fixtures = list(Fixture.objects.select_related("home_team", "away_team").order_by("kickoff", "id"))
        issues = 0

        for eh, ea, hg, ag, expected_result in VERIFIED:
            matches = [f for f in all_fixtures if match(f.home_team.name, eh) and match(f.away_team.name, ea)]
            self.stdout.write(f"=== {eh} vs {ea} | EXPECTED {hg}-{ag} {expected_result} | fixtures={len(matches)} ===")
            if not matches:
                issues += 1
                self.stdout.write(self.style.ERROR("NO FIXTURE MATCH"))
                continue

            for f in matches:
                score_ok = f.home_goals == hg and f.away_goals == ag
                preds = list(Prediction.objects.filter(fixture=f, market__iexact="BTTS").order_by("id"))
                self.stdout.write(
                    f"FIXTURE id={f.id} ext={f.external_id} kickoff={f.kickoff.isoformat()} "
                    f"teams='{f.home_team.name}' vs '{f.away_team.name}' db_score={f.home_goals}-{f.away_goals} "
                    f"score_ok={score_ok} btts_predictions={len(preds)}"
                )
                if not score_ok:
                    issues += 1
                if not preds:
                    self.stdout.write("  NO BTTS PREDICTION")
                for p in preds:
                    ledger = PremiumPublicationLedger.objects.filter(prediction=p).first()
                    outcome = PredictionOutcome.objects.filter(prediction=p).first()
                    ledger_txt = "NONE" if not ledger else (
                        f"id={ledger.id} date={ledger.target_date} rank={ledger.published_rank} "
                        f"tier={ledger.premium_tier} odds={ledger.odds} model={ledger.model_version}"
                    )
                    outcome_txt = "NONE" if not outcome else (
                        f"id={outcome.id} result={outcome.result} score={outcome.home_goals}-{outcome.away_goals} "
                        f"reason={outcome.settlement_reason}"
                    )
                    self.stdout.write(
                        f"  PRED id={p.id} model={p.model_version} tier={p.tier} prob={p.probability} "
                        f"score={p.score} | LEDGER {ledger_txt} | OUTCOME {outcome_txt}"
                    )
                    if ledger and (not outcome or outcome.result != expected_result or outcome.home_goals != hg or outcome.away_goals != ag):
                        issues += 1
                        self.stdout.write(self.style.ERROR("  !! LEDGER-BACKED OUTCOME MISMATCH"))
            self.stdout.write("")

        if issues:
            self.stdout.write(self.style.WARNING(f"AUDIT RESULT: {issues} discrepancy signal(s). Do NOT freeze challenger yet."))
        else:
            self.stdout.write(self.style.SUCCESS("AUDIT RESULT: CLEAN. All eight verified fixtures/outcomes are consistent."))
