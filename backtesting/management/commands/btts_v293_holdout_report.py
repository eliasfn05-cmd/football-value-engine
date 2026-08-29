from django.core.management.base import BaseCommand

from backtesting.models import BttsV293HoldoutSnapshot, PredictionOutcome

VERSION = "V2.9.3-FROZEN"


class Command(BaseCommand):
    help = "Report true forward holdout performance for immutable V2.9.3 A#1 snapshots."

    def handle(self, *args, **options):
        rows = list(BttsV293HoldoutSnapshot.objects.filter(challenger_version=VERSION).select_related(
            "fixture", "fixture__home_team", "fixture__away_team", "prediction"
        ).order_by("target_date", "rank", "id"))
        wins = losses = one = zz = pending = priced = 0
        profit = 0.0
        self.stdout.write(f"BTTS V2.9.3 HOLDOUT REPORT | version={VERSION} snapshots={len(rows)}")
        self.stdout.write("IMMUTABLE FORWARD SNAPSHOTS ONLY | no retrospective backfill.\n")
        for row in rows:
            outcome = PredictionOutcome.objects.filter(prediction=row.prediction).first()
            status = "PENDING" if not outcome else outcome.result
            score = "-" if not outcome or outcome.home_goals is None else f"{outcome.home_goals}-{outcome.away_goals}"
            if status == "WIN":
                wins += 1
            elif status == "LOSS":
                losses += 1
                if outcome.home_goals == 0 and outcome.away_goals == 0:
                    zz += 1
                elif (outcome.home_goals == 0) != (outcome.away_goals == 0):
                    one += 1
            else:
                pending += 1
            if status in ("WIN", "LOSS") and row.market_odds and float(row.market_odds) > 1:
                priced += 1
                profit += float(row.market_odds) - 1 if status == "WIN" else -1
            f = row.fixture
            self.stdout.write(
                f"{row.target_date} A#{row.rank} | {f.home_team.name} vs {f.away_team.name} | "
                f"v293={float(row.recalibrated_score):.2f} odds={row.market_odds} | {status} {score}"
            )
        settled = wins + losses
        hit = wins / settled if settled else 0.0
        roi = profit / priced if priced else 0.0
        self.stdout.write("")
        self.stdout.write(
            f"SUMMARY settled={settled} W={wins} L={losses} hit={hit:.4f} roi={roi:+.4f} "
            f"one_sided={one} zero_zero={zz} pending={pending} priced={priced}"
        )
        if settled < 20:
            self.stdout.write(self.style.WARNING(f"HOLDOUT INCOMPLETE: {settled}/20 minimum observations. Do not promote V2.9.3."))
        elif hit >= .65 and roi > 0:
            self.stdout.write(self.style.WARNING("HOLDOUT CANDIDATE: hit>=65% and ROI>0. Compare against production V2.9.1 before promotion."))
        else:
            self.stdout.write(self.style.SUCCESS("HOLDOUT DOES NOT SUPPORT PROMOTION YET."))
