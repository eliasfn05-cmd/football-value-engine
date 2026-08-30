from django.core.management.base import BaseCommand
from django.db.models import Q

from backtesting.models import PredictionOutcome
from engine.btts_v291_policy import tier_a_decision_v291
from engine.models import Fixture, Prediction


class Command(BaseCommand):
    help = "Leakage-safe Tier A audit of recent scoring continuity for BTTS candidates."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10000)
        parser.add_argument("--show", type=int, default=50)

    def _profile(self, team, fixture):
        qs = Fixture.objects.filter(
            Q(home_team=team) | Q(away_team=team),
            kickoff__lt=fixture.kickoff,
            home_goals__isnull=False,
            away_goals__isnull=False,
        ).order_by("-kickoff")[:5]
        gf = []
        for prev in qs:
            gf.append(int(prev.home_goals or 0) if prev.home_team_id == team.id else int(prev.away_goals or 0))
        blanks = 0
        for g in gf:
            if g == 0:
                blanks += 1
            else:
                break
        return {
            "n": len(gf),
            "score_rate": (sum(g > 0 for g in gf) / len(gf)) if gf else None,
            "last_blank": int(bool(gf and gf[0] == 0)),
            "last5_scored": sum(g > 0 for g in gf),
            "consecutive_blanks": blanks,
        }

    def handle(self, *args, **opts):
        qs = Prediction.objects.filter(
            market__iexact="BTTS",
            outcome__result__in=["WIN", "LOSS"],
            fixture__home_goals__isnull=False,
            fixture__away_goals__isnull=False,
        ).select_related("fixture", "fixture__home_team", "fixture__away_team", "outcome").order_by("fixture__kickoff")[: opts["limit"]]

        rows = []
        for pred in qs:
            decision = tier_a_decision_v291(pred)
            if decision and getattr(decision, "blocked", False):
                continue
            f = pred.fixture
            hp = self._profile(f.home_team, f)
            ap = self._profile(f.away_team, f)
            weak_recent = int((hp["last5_scored"] < 4) or (ap["last5_scored"] < 4))
            last_blank = int(hp["last_blank"] or ap["last_blank"])
            repeat_blank = int(hp["consecutive_blanks"] >= 2 or ap["consecutive_blanks"] >= 2)
            blank_x_weak = int(last_blank and weak_recent)
            low60 = int(
                (hp["score_rate"] is not None and hp["score_rate"] < .60)
                or (ap["score_rate"] is not None and ap["score_rate"] < .60)
            )
            o = pred.outcome
            one = int(o.result == "LOSS" and ((o.home_goals == 0) != (o.away_goals == 0)))
            zz = int(o.result == "LOSS" and o.home_goals == 0 and o.away_goals == 0)
            rows.append((o.result, weak_recent, last_blank, repeat_blank, blank_x_weak, low60, one, zz, pred, hp, ap))

        wins = [r for r in rows if r[0] == "WIN"]
        losses = [r for r in rows if r[0] == "LOSS"]
        one_losses = [r for r in rows if r[6]]
        zz_losses = [r for r in rows if r[7]]

        self.stdout.write(f"BTTS V2.9.6 TIER A ATTACK CONTINUITY AUDIT | settled={len(rows)} wins={len(wins)} losses={len(losses)}")
        self.stdout.write("READ-ONLY | PRE-KICKOFF ONLY | V2.9.1 Tier A gate applied historically; no production changes.\n")

        for label, idx in (
            ("WEAK_RECENT_SCORING", 1),
            ("ANY_LAST_MATCH_BLANK", 2),
            ("CONSECUTIVE_BLANKS_GE2", 3),
            ("BLANK_X_WEAK_SCORING", 4),
            ("CURRENT_SCORE_RATE_LT60", 5),
        ):
            w = sum(r[idx] for r in wins)
            l = sum(r[idx] for r in losses)
            one = sum(r[idx] for r in one_losses)
            zz = sum(r[idx] for r in zz_losses)
            wr = w / len(wins) if wins else 0
            lr = l / len(losses) if losses else 0
            oner = one / len(one_losses) if one_losses else 0
            zzr = zz / len(zz_losses) if zz_losses else 0
            self.stdout.write(
                f"{label:28} WIN={w}/{len(wins)}({wr:.2f}) LOSS={l}/{len(losses)}({lr:.2f}) sep={lr-wr:+.2f} "
                f"ONE={one}/{len(one_losses)}({oner:.2f}) 0-0={zz}/{len(zz_losses)}({zzr:.2f})"
            )

        self.stdout.write(f"\nLOSS TYPES | one_sided={len(one_losses)} zero_zero={len(zz_losses)}")
        shown = 0
        for r in losses:
            if not any(r[1:6]):
                continue
            _, weak, last_blank, repeat, inter, low60, one, zz, pred, hp, ap = r
            f = pred.fixture
            self.stdout.write(
                f"LOSS {'ONE' if one else '0-0' if zz else 'OTHER'} | pred={pred.id} | {f.home_team.name} vs {f.away_team.name} | "
                f"home(n={hp['n']} rate={hp['score_rate']} L5={hp['last5_scored']} blanks={hp['consecutive_blanks']}) "
                f"away(n={ap['n']} rate={ap['score_rate']} L5={ap['last5_scored']} blanks={ap['consecutive_blanks']}) | "
                f"weak={weak} last_blank={last_blank} repeat={repeat} interaction={inter} low60={low60}"
            )
            shown += 1
            if shown >= opts["show"]:
                break

        self.stdout.write("\nDECISION RULE: only add a soft penalty if the signal separates Tier A losses from wins and especially one-sided losses without excessive WIN exposure. No hard gate from this audit alone.")
