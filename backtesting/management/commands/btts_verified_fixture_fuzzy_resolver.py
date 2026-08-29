from difflib import SequenceMatcher
import re
import unicodedata

from django.core.management.base import BaseCommand

from engine.models import Fixture, Prediction, PremiumPublicationLedger
from backtesting.models import PredictionOutcome


VERIFIED = [
    ("Stockholm Internationale", "Jarfalla", 2, 0, "LOSS"),
    ("Audax Italiano", "U. La Calera", 0, 0, "LOSS"),
    ("Deportivo Cuenca", "Mushuc Runa", 0, 0, "LOSS"),
    ("RKC Waalwijk", "Jong PSV", 2, 2, "WIN"),
    ("Winterthur", "Lausanne Ouchy", 1, 1, "WIN"),
]

STOP = {"fc", "cf", "club", "de", "del", "la", "the", "sc", "ac", "cd", "u"}


def norm(v):
    v = unicodedata.normalize("NFKD", str(v or ""))
    v = "".join(c for c in v if not unicodedata.combining(c)).lower()
    v = re.sub(r"[^a-z0-9]+", " ", v).strip()
    return " ".join(x for x in v.split() if x not in STOP)


def sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    seq = SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    jacc = len(ta & tb) / max(1, len(ta | tb))
    contains = 1.0 if (a in b or b in a) else 0.0
    return 0.55 * seq + 0.30 * jacc + 0.15 * contains


class Command(BaseCommand):
    help = "Read-only fuzzy resolver for verified BTTS fixtures that exact-name audit could not find."

    def add_arguments(self, parser):
        parser.add_argument("--top", type=int, default=5)
        parser.add_argument("--year", type=int, default=2026)
        parser.add_argument("--month", type=int, default=8)

    def handle(self, *args, **opts):
        topn = max(1, min(int(opts["top"]), 15))
        year = int(opts["year"])
        month = int(opts["month"])
        fixtures = list(
            Fixture.objects.filter(kickoff__year=year, kickoff__month=month)
            .select_related("home_team", "away_team")
            .order_by("kickoff", "id")
        )

        self.stdout.write("BTTS VERIFIED FIXTURE FUZZY RESOLVER | READ-ONLY")
        self.stdout.write(f"Scope: {year}-{month:02d} | fixtures={len(fixtures)} | top={topn}\n")

        for eh, ea, hg, ag, expected_result in VERIFIED:
            ranked = []
            for f in fixtures:
                hs = sim(f.home_team.name, eh)
                as_ = sim(f.away_team.name, ea)
                pair = (hs + as_) / 2.0
                # Small evidence boost for a score already matching the verified result.
                score_boost = 0.06 if f.home_goals == hg and f.away_goals == ag else 0.0
                btts_preds = Prediction.objects.filter(fixture=f, market__iexact="BTTS").count()
                pred_boost = 0.03 if btts_preds else 0.0
                ranked.append((pair + score_boost + pred_boost, hs, as_, f, btts_preds))
            ranked.sort(key=lambda x: (x[0], x[3].kickoff), reverse=True)

            self.stdout.write(f"=== EXPECTED {eh} vs {ea} | {hg}-{ag} {expected_result} ===")
            for idx, (score, hs, as_, f, pred_count) in enumerate(ranked[:topn], 1):
                preds = list(Prediction.objects.filter(fixture=f, market__iexact="BTTS").order_by("id"))
                ledger_ids = []
                outcome_bits = []
                for p in preds:
                    ledger = PremiumPublicationLedger.objects.filter(prediction=p).first()
                    outcome = PredictionOutcome.objects.filter(prediction=p).first()
                    if ledger:
                        ledger_ids.append(str(ledger.id))
                    if outcome:
                        outcome_bits.append(f"p{p.id}:{outcome.result}:{outcome.home_goals}-{outcome.away_goals}")
                self.stdout.write(
                    f"#{idx} match={score:.3f} home={hs:.3f} away={as_:.3f} | "
                    f"fixture_id={f.id} ext={f.external_id} kickoff={f.kickoff.isoformat()} | "
                    f"'{f.home_team.name}' vs '{f.away_team.name}' | db={f.home_goals}-{f.away_goals} | "
                    f"BTTS_preds={pred_count} ledgers={','.join(ledger_ids) or 'NONE'} outcomes={';'.join(outcome_bits) or 'NONE'}"
                )
            self.stdout.write("")

        self.stdout.write("Use the top candidates only to identify exact fixture IDs. This command makes no changes.")
