from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db.models import Q

from engine.btts_v25_policy import anti_zero_metrics
from engine.btts_v291_policy import tier_a_decision_v291
from engine.models import Fixture, Prediction


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _blocked(d):
    return bool(d and getattr(d, "blocked", False))


def _previous(team, fixture, role=None, limit=10):
    base = dict(kickoff__lt=fixture.kickoff, home_goals__isnull=False, away_goals__isnull=False)
    if role == "home":
        qs = Fixture.objects.filter(home_team=team, **base)
    elif role == "away":
        qs = Fixture.objects.filter(away_team=team, **base)
    else:
        qs = Fixture.objects.filter(Q(home_team=team) | Q(away_team=team), **base)
    return list(qs.order_by("-kickoff")[:limit])


def _clean_sheet_rate(team, fixture):
    games = _previous(team, fixture, limit=10)
    if not games:
        return None
    cs = 0
    for g in games:
        ga = g.away_goals if g.home_team_id == team.id else g.home_goals
        cs += int(ga == 0)
    return cs / len(games)


def _snapshot(p):
    m = anti_zero_metrics(p)
    if not m.get("available"):
        return None
    f = p.fixture
    home, away = m["home"], m["away"]
    ho, ao = m["home_overall"], m["away_overall"]
    weak_home = _f(home.get("score_probability")) <= _f(away.get("score_probability"))
    weak = home if weak_home else away
    weak_o = ho if weak_home else ao
    hcs = _clean_sheet_rate(f.home_team, f)
    acs = _clean_sheet_rate(f.away_team, f)
    if hcs is None or acs is None:
        return None
    return {
        "raw": _f(p.score), "odds": _f(p.market_odds),
        "emp": _f(m.get("empirical_btts")), "cons": _f(m.get("consensus_probability")),
        "cal": _f(m.get("calibrated_probability")), "weakp": _f(m.get("weakest_score_probability")),
        "weak_btts5": _f(weak_o.get("last5_btts")), "weak_scored5": _f(weak_o.get("last5_scored")),
        "max_overall_cs": max(hcs, acs),
    }


def _v293(m):
    s = 100*(.35*m["emp"]+.25*m["cons"]+.20*m["cal"]+.20*m["weakp"])
    if m["raw"] >= 85 and m["emp"] < .68: s -= 10
    if m["raw"] >= 85 and m["cal"] < .72: s -= 4
    if m["raw"] >= 90 and m["cons"] < .73: s -= 4
    if m["emp"] >= .80: s += 3
    if m["cons"] >= .75: s += 2
    if m["weakp"] >= .80: s += 2
    return s


def _v294(m):
    penalty, flags = 0.0, 0
    if m["weak_btts5"] < 4: penalty += 8; flags += 1
    if m["weak_scored5"] < 4: penalty += 5; flags += 1
    if m["emp"] < .68: penalty += min(8, (.68-m["emp"])*25); flags += 1
    if m["emp"] < .60: penalty += 4
    if flags >= 2: penalty += 4
    return _v293(m)-penalty


def _v295(m):
    """Combined challenger: defensive strength only matters in interaction.

    We deliberately do not penalize clean sheets alone. Audit V2.9.5 showed
    overall CS >= .30 separated losses somewhat, but role CS did not. Hence
    defensive risk is activated only when recent bilateral/empirical evidence
    is also weak.
    """
    base = _v294(m)
    cs = m["max_overall_cs"] >= .30
    weak_btts = m["weak_btts5"] < 4
    low_emp = m["emp"] < .68
    weak_score = m["weak_scored5"] < 4
    penalty = 0.0
    flags = sum((cs, weak_btts, low_emp, weak_score))
    if cs and weak_btts: penalty += 5.0
    if cs and low_emp: penalty += 4.0
    if cs and weak_score: penalty += 3.0
    if cs and sum((weak_btts, low_emp, weak_score)) >= 2: penalty += 3.0
    return base-penalty, penalty, flags


def _summary(rows):
    n=len(rows); w=sum(r["won"] for r in rows); one=sum(r["one"] for r in rows); zz=sum(r["zz"] for r in rows)
    priced=[r for r in rows if r["m"]["odds"]>1]
    profit=sum((r["m"]["odds"]-1 if r["won"] else -1) for r in priced)
    return {"n":n,"w":w,"hit":w/n if n else 0,"roi":profit/len(priced) if priced else 0,"one":one,"zz":zz}


def _fmt(s):
    return f"n={s['n']} W={s['w']} L={s['n']-s['w']} hit={s['hit']:.4f} roi={s['roi']:+.4f} one={s['one']} 0-0={s['zz']}"


class Command(BaseCommand):
    help="Audit-only walk-forward V2.9.3 vs V2.9.4 vs V2.9.5 Combined One-Sided Risk."

    def add_arguments(self, p):
        p.add_argument("--limit", type=int, default=10000); p.add_argument("--windows", type=int, default=4); p.add_argument("--show", type=int, default=50)

    def handle(self, *args, **o):
        limit=max(100,min(o["limit"],10000)); windows=max(2,min(o["windows"],10)); show=max(1,min(o["show"],100))
        qs=Prediction.objects.filter(market__iexact="BTTS",fixture__home_goals__isnull=False,fixture__away_goals__isnull=False).select_related("fixture","fixture__home_team","fixture__away_team").order_by("-fixture__kickoff","-created_at")[:limit]
        seen=set(); rows=[]; unavailable=0
        for p in qs:
            if p.fixture_id in seen: continue
            seen.add(p.fixture_id)
            if _blocked(tier_a_decision_v291(p)): continue
            m=_snapshot(p)
            if not m: unavailable+=1; continue
            f=p.fixture; won=f.home_goals>0 and f.away_goals>0; one=(not won and ((f.home_goals==0)!=(f.away_goals==0))); zz=(not won and f.home_goals==0 and f.away_goals==0)
            s295,pen,flags=_v295(m)
            rows.append({"p":p,"date":f.kickoff.date(),"m":m,"s293":_v293(m),"s294":_v294(m),"s295":s295,"pen":pen,"flags":flags,"won":won,"one":one,"zz":zz})
        self.stdout.write(self.style.SUCCESS(f"BTTS V2.9.5 COMBINED ONE-SIDED RISK WALK-FORWARD | tier_a={len(rows)} unavailable={unavailable}"))
        self.stdout.write("SOURCE=PRE_KICKOFF_ONLY | audit-only; NO production changes.")
        days=defaultdict(list)
        for r in rows: days[r["date"]].append(r)
        tops={"293":[],"294":[],"295":[]}; changes=[]
        for d in sorted(days):
            g=days[d]; a=max(g,key=lambda r:(r["s293"],r["m"]["raw"])); b=max(g,key=lambda r:(r["s294"],r["s293"])); c=max(g,key=lambda r:(r["s295"],r["s294"]))
            tops["293"].append(a); tops["294"].append(b); tops["295"].append(c)
            if b["p"].fixture_id != c["p"].fixture_id: changes.append((d,b,c))
        s293,s294,s295=map(_summary,(tops["293"],tops["294"],tops["295"]))
        self.stdout.write("\nPREMIUM A#1 DAILY COMPARISON")
        self.stdout.write("RECAL_V293  "+_fmt(s293)); self.stdout.write("BILAT_V294  "+_fmt(s294)); self.stdout.write("COMBO_V295  "+_fmt(s295))
        self.stdout.write(f"DELTA 295-294 hit={s295['hit']-s294['hit']:+.4f} roi={s295['roi']-s294['roi']:+.4f} one={s295['one']-s294['one']:+d} 0-0={s295['zz']-s294['zz']:+d} changes={len(changes)}")
        self.stdout.write("\nRANK CHANGES | V294 -> V295")
        for d,a,b in changes[:show]:
            af=a["p"].fixture; bf=b["p"].fixture
            self.stdout.write(f"{d} | OLD {'WIN' if a['won'] else 'LOSS'} {af.home_team.name} vs {af.away_team.name} v294={a['s294']:.2f} cs={a['m']['max_overall_cs']:.2f} btts5={a['m']['weak_btts5']:.0f} emp={a['m']['emp']:.3f} -> NEW {'WIN' if b['won'] else 'LOSS'} {bf.home_team.name} vs {bf.away_team.name} v295={b['s295']:.2f}")
        self.stdout.write("\nTEMPORAL WINDOWS | V294 vs V295")
        n=len(tops["295"]); w=min(windows,n) if n else 0
        if w>=2:
            size=n//w; rem=n%w; start=0
            for i in range(w):
                width=size+(1 if i<rem else 0); end=start+width; a=tops["294"][start:end]; b=tops["295"][start:end]
                self.stdout.write(f"WINDOW {i+1} {b[0]['date']}->{b[-1]['date']} | V294 {_fmt(_summary(a))} | V295 {_fmt(_summary(b))}"); start=end
        promote=(s295["n"]==s294["n"] and s295["hit"]>=s294["hit"] and s295["roi"]>=s294["roi"] and s295["one"]<=s294["one"] and s295["zz"]<=s294["zz"] and (s295["hit"]>s294["hit"] or s295["roi"]>s294["roi"] or s295["one"]<s294["one"]))
        self.stdout.write("")
        if promote: self.stdout.write(self.style.WARNING("CANDIDATE SIGNAL: V2.9.5 mejora retrospectivamente. NO promover aun; exigir estabilidad temporal y holdout futuro."))
        else: self.stdout.write(self.style.SUCCESS("NO PROMOTION: V2.9.5 no mejora simultaneamente hit/ROI/one-sided. Mantener baseline/challengers actuales."))
