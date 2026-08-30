from django.core.management.base import BaseCommand
from django.db.models import Q

from backtesting.models import PredictionOutcome
from engine.models import Fixture, PremiumPublicationLedger


class Command(BaseCommand):
    help = "Leakage-safe audit of early-season scoring continuity risk for official BTTS Premium picks."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10000)
        parser.add_argument("--show", type=int, default=50)

    def _season_profile(self, team, fixture):
        qs = Fixture.objects.filter(
            Q(home_team=team) | Q(away_team=team),
            kickoff__lt=fixture.kickoff,
            home_goals__isnull=False,
            away_goals__isnull=False,
            season=fixture.season,
        ).order_by("-kickoff")[:5]
        gf = []
        for prev in qs:
            gf.append(int(prev.home_goals or 0) if prev.home_team_id == team.id else int(prev.away_goals or 0))
        blanks = 0
        for goals in gf:
            if goals == 0:
                blanks += 1
            else:
                break
        return {
            "n": len(gf),
            "last_scored": int(bool(gf and gf[0] > 0)),
            "last_blank": int(bool(gf and gf[0] == 0)),
            "consecutive_blanks": blanks,
            "score_rate": (sum(g > 0 for g in gf) / len(gf)) if gf else None,
        }

    def handle(self, *args, **opts):
        ledgers = PremiumPublicationLedger.objects.filter(market__iexact="BTTS").select_related(
            "prediction", "prediction__fixture", "prediction__fixture__home_team", "prediction__fixture__away_team"
        ).order_by("published_at")[: opts["limit"]]
        rows = []
        for ledger in ledgers:
            pred = ledger.prediction
            fixture = pred.fixture
            outcome = PredictionOutcome.objects.filter(prediction=pred, result__in=["WIN", "LOSS"]).first()
            if not outcome or outcome.home_goals is None or outcome.away_goals is None:
                continue
            hp = self._season_profile(fixture.home_team, fixture)
            ap = self._season_profile(fixture.away_team, fixture)
            weakest = hp if (hp["score_rate"] if hp["score_rate"] is not None else 1) <= (ap["score_rate"] if ap["score_rate"] is not None else 1) else ap
            early_blank = int((hp["n"] <= 2 and hp["last_blank"]) or (ap["n"] <= 2 and ap["last_blank"]))
            low_cont = int((hp["n"] >= 2 and hp["score_rate"] < .50) or (ap["n"] >= 2 and ap["score_rate"] < .50))
            repeat_blank = int(hp["consecutive_blanks"] >= 2 or ap["consecutive_blanks"] >= 2)
            one = int(outcome.result == "LOSS" and ((outcome.home_goals == 0) != (outcome.away_goals == 0)))
            zero = int(outcome.result == "LOSS" and outcome.home_goals == 0 and outcome.away_goals == 0)
            rows.append((outcome.result, early_blank, low_cont, repeat_blank, one, zero, fixture, hp, ap))

        self.stdout.write(f"BTTS V2.9.6 EARLY-SEASON ATTACK CONTINUITY AUDIT | settled={len(rows)}")
        self.stdout.write("READ-ONLY | PRE-KICKOFF ONLY | no production gate/ranking changes.\n")
        wins = [r for r in rows if r[0] == "WIN"]
        losses = [r for r in rows if r[0] == "LOSS"]
        for label, idx in (("EARLY_SEASON_LAST_BLANK", 1), ("CURRENT_SEASON_SCORE_RATE_LT50", 2), ("CONSECUTIVE_BLANKS_GE2", 3)):
            w = sum(r[idx] for r in wins); l = sum(r[idx] for r in losses)
            wr = w / len(wins) if wins else 0; lr = l / len(losses) if losses else 0
            self.stdout.write(f"{label:34} WIN={w}/{len(wins)}({wr:.2f}) LOSS={l}/{len(losses)}({lr:.2f}) separation={lr-wr:+.2f}")
        self.stdout.write("")
        self.stdout.write(f"LOSS TYPES | one_sided={sum(r[4] for r in rows)} zero_zero={sum(r[5] for r in rows)}")
        shown = 0
        for r in losses:
            if not any(r[1:4]):
                continue
            _, early, low, repeat, one, zero, f, hp, ap = r
            self.stdout.write(
                f"LOSS {'ONE' if one else '0-0' if zero else 'OTHER'} | {f.home_team.name} vs {f.away_team.name} | "
                f"home(n={hp['n']} rate={hp['score_rate']} blanks={hp['consecutive_blanks']}) "
                f"away(n={ap['n']} rate={ap['score_rate']} blanks={ap['consecutive_blanks']}) | early={early} low={low} repeat={repeat}"
            )
            shown += 1
            if shown >= opts["show"]:
                break
        self.stdout.write("\nINTERPRETATION: validate separation before any hard gate. Tottenham-type early-season blank risk should be a soft penalty first, not hindsight blacklist.")
