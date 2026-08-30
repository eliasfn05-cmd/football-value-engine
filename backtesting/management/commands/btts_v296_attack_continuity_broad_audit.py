from django.core.management.base import BaseCommand
from django.db.models import Q

from backtesting.models import PredictionOutcome
from engine.models import Fixture, Prediction


class Command(BaseCommand):
    help = "Leakage-safe broad audit of attack-continuity risk across all settled BTTS predictions, not only official Premium ledger rows."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10000)
        parser.add_argument("--show", type=int, default=50)

    def _profile(self, team, fixture):
        base = Fixture.objects.filter(
            Q(home_team=team) | Q(away_team=team),
            kickoff__lt=fixture.kickoff,
            home_goals__isnull=False,
            away_goals__isnull=False,
        )
        # Prefer same competition/season when populated, but never silently erase
        # the sample because historical season metadata is sparse/inconsistent.
        scoped = base
        if getattr(fixture, "season", None):
            same_season = base.filter(season=fixture.season)
            if same_season.exists():
                scoped = same_season
        if getattr(fixture, "competition_ref_id", None):
            same_comp = scoped.filter(competition_ref_id=fixture.competition_ref_id)
            if same_comp.exists():
                scoped = same_comp

        qs = scoped.order_by("-kickoff")[:5]
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
            "last_blank": int(bool(gf and gf[0] == 0)),
            "consecutive_blanks": blanks,
            "score_rate": (sum(g > 0 for g in gf) / len(gf)) if gf else None,
            "last5_scored": sum(g > 0 for g in gf),
        }

    def handle(self, *args, **opts):
        outcomes = PredictionOutcome.objects.filter(
            prediction__market__iexact="BTTS",
            result__in=["WIN", "LOSS"],
            home_goals__isnull=False,
            away_goals__isnull=False,
        ).select_related(
            "prediction", "prediction__fixture", "prediction__fixture__home_team", "prediction__fixture__away_team"
        ).order_by("prediction__fixture__kickoff")[: opts["limit"]]

        rows = []
        for outcome in outcomes:
            pred = outcome.prediction
            f = pred.fixture
            hp = self._profile(f.home_team, f)
            ap = self._profile(f.away_team, f)
            if not hp["n"] or not ap["n"]:
                continue
            min_n = min(hp["n"], ap["n"])
            early_last_blank = int(min_n <= 2 and (hp["last_blank"] or ap["last_blank"]))
            any_last_blank = int(hp["last_blank"] or ap["last_blank"])
            low_score_rate_60 = int(
                (hp["n"] >= 2 and hp["score_rate"] < .60) or
                (ap["n"] >= 2 and ap["score_rate"] < .60)
            )
            repeat_blank = int(hp["consecutive_blanks"] >= 2 or ap["consecutive_blanks"] >= 2)
            weak_recent_scoring = int(hp["last5_scored"] < min(4, hp["n"]) or ap["last5_scored"] < min(4, ap["n"]))
            interaction = int(any_last_blank and (low_score_rate_60 or weak_recent_scoring))
            one = int(outcome.result == "LOSS" and ((outcome.home_goals == 0) != (outcome.away_goals == 0)))
            zero = int(outcome.result == "LOSS" and outcome.home_goals == 0 and outcome.away_goals == 0)
            rows.append((outcome.result, early_last_blank, any_last_blank, low_score_rate_60, repeat_blank, weak_recent_scoring, interaction, one, zero, f, hp, ap, pred))

        wins = [r for r in rows if r[0] == "WIN"]
        losses = [r for r in rows if r[0] == "LOSS"]
        self.stdout.write(f"BTTS V2.9.6 BROAD ATTACK CONTINUITY AUDIT | settled={len(rows)} wins={len(wins)} losses={len(losses)}")
        self.stdout.write("READ-ONLY | PRE-KICKOFF ONLY | all settled BTTS predictions; no production changes.\n")

        flags = (
            ("EARLY_SEASON_LAST_BLANK", 1),
            ("ANY_LAST_MATCH_BLANK", 2),
            ("CURRENT_SCORE_RATE_LT60", 3),
            ("CONSECUTIVE_BLANKS_GE2", 4),
            ("WEAK_RECENT_SCORING", 5),
            ("BLANK_X_WEAK_SCORING", 6),
        )
        for label, idx in flags:
            w = sum(r[idx] for r in wins); l = sum(r[idx] for r in losses)
            wr = w / len(wins) if wins else 0; lr = l / len(losses) if losses else 0
            self.stdout.write(f"{label:32} WIN={w}/{len(wins)}({wr:.2f}) LOSS={l}/{len(losses)}({lr:.2f}) separation={lr-wr:+.2f}")

        self.stdout.write("")
        self.stdout.write(f"LOSS TYPES | one_sided={sum(r[7] for r in rows)} zero_zero={sum(r[8] for r in rows)}")
        shown = 0
        for r in losses:
            if not r[6]:
                continue
            _, early, lastb, low, repeat, weak, inter, one, zero, f, hp, ap, pred = r
            self.stdout.write(
                f"LOSS {'ONE' if one else '0-0' if zero else 'OTHER'} | pred={pred.id} | {f.home_team.name} vs {f.away_team.name} | "
                f"home(n={hp['n']} rate={hp['score_rate']:.2f} L5={hp['last5_scored']} blanks={hp['consecutive_blanks']}) "
                f"away(n={ap['n']} rate={ap['score_rate']:.2f} L5={ap['last5_scored']} blanks={ap['consecutive_blanks']}) | "
                f"early={early} last_blank={lastb} low60={low} repeat={repeat} weak={weak} interaction={inter}"
            )
            shown += 1
            if shown >= opts["show"]:
                break

        self.stdout.write("\nDECISION RULE: only consider a soft penalty if BLANK_X_WEAK_SCORING or another flag shows repeatable positive loss separation without excessive WIN exposure.")
