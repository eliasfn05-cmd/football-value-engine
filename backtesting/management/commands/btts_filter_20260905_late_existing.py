from __future__ import annotations

from backtesting.management.commands.btts_filter_20260905_late import DAY, TARGETS, sim, decision_text
from django.core.management.base import BaseCommand
from engine.btts_v25_policy import anti_zero_metrics
from engine.btts_v291_policy import tier_a_decision_v291
from engine.btts_v299_policy import tier_a_decision_v299, anti_zero_decision_v299
from engine.competition_quality import classify_competition
from engine.models import Fixture, Prediction

class Command(BaseCommand):
    help = "Fast read-only ranking of existing BTTS predictions for late Sep 5 screenshot targets."

    def handle(self, *args, **options):
        fixtures = list(Fixture.objects.filter(kickoff__date=DAY).select_related("home_team", "away_team", "competition_ref"))
        rows=[]
        self.stdout.write(f"BTTS FAST EXISTING | {DAY} | targets={len(TARGETS)} db_fixtures={len(fixtures)}")
        self.stdout.write("POLICY | V2.9.1 + V2.9.9 | odds excluded | existing predictions only")
        for home,away in TARGETS:
            scored=sorted([((sim(home,f.home_team.name)+sim(away,f.away_team.name))/2.0,f) for f in fixtures],key=lambda x:x[0],reverse=True)
            best,fixture=scored[0] if scored else (0.0,None)
            if fixture is None or best < 0.68:
                self.stdout.write(f"MISSING | {home} vs {away} | best={best:.2f}"); continue
            if classify_competition(fixture).excluded:
                self.stdout.write(f"EXCLUDED | {fixture.home_team.name} vs {fixture.away_team.name} | match={best:.2f}"); continue
            prediction=Prediction.objects.filter(fixture=fixture,market__iexact="BTTS").order_by("-created_at","-id").first()
            if prediction is None:
                self.stdout.write(f"NO_PRED | {fixture.home_team.name} vs {fixture.away_team.name} | match={best:.2f}"); continue
            metrics=anti_zero_metrics(prediction)
            d291=tier_a_decision_v291(prediction); d299a=tier_a_decision_v299(prediction); d299b=anti_zero_decision_v299(prediction)
            available=bool(metrics.get("available")); weak=float(metrics.get("weakest_score_probability",0) or 0)
            cal=float(metrics.get("calibrated_probability",0) or 0); cons=float(metrics.get("consensus_probability",0) or 0); emp=float(metrics.get("empirical_btts",0) or 0)
            ho=metrics.get("home_overall") or {}; ao=metrics.get("away_overall") or {}
            fts=max(float(ho.get("failed_to_score_rate",1) or 0),float(ao.get("failed_to_score_rate",1) or 0)) if available else 1.0
            l5s=min(int(ho.get("last5_scored",0) or 0),int(ao.get("last5_scored",0) or 0)) if available else 0
            tier="A" if d291 is None and d299a is None else ("B" if d299b is None else "X")
            rank=(100 if tier=="A" else 65 if tier=="B" else 0)+cons*35+weak*30+cal*20+emp*15-fts*20
            rows.append((rank,tier,fixture,prediction,weak,cal,cons,emp,fts,l5s,d291,d299a,d299b,best))
        rows.sort(key=lambda x:x[0],reverse=True)
        self.stdout.write("\nRANKED")
        for rank,tier,fixture,prediction,weak,cal,cons,emp,fts,l5s,d291,d299a,d299b,match in rows:
            self.stdout.write(f"{tier} | {fixture.home_team.name} vs {fixture.away_team.name} | rank={rank:.2f} raw={float(prediction.score or 0):.1f} prob={float(prediction.probability or 0):.3f} weak={weak:.3f} cal={cal:.3f} cons={cons:.3f} emp={emp:.3f} maxFTS={fts:.3f} minL5scored={l5s}/5 v291={decision_text(d291)} v299A={decision_text(d299a)} v299B={decision_text(d299b)} match={match:.2f}")
