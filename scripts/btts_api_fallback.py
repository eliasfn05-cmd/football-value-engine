import os, json, math, re, sys, time
from difflib import SequenceMatcher
import requests

BASE = "https://v3.football.api-sports.io"
KEY = os.environ.get("API_FOOTBALL_KEY", "")
HEADERS = {"x-apisports-key": KEY}

def norm(s):
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())

def sim(a,b):
    a,b=norm(a),norm(b)
    if a==b: return 1.0
    if a in b or b in a: return 0.92
    return SequenceMatcher(None,a,b).ratio()

def get(path, params):
    r=requests.get(BASE+path, headers=HEADERS, params=params, timeout=25)
    r.raise_for_status()
    data=r.json()
    if data.get("errors"):
        raise RuntimeError(str(data["errors"]))
    return data.get("response",[])

def finished_rows(team_id, last=20):
    rows=get('/fixtures', {'team':team_id,'last':last,'status':'FT'})
    out=[]
    for x in rows:
        gh=x.get('goals',{}).get('home'); ga=x.get('goals',{}).get('away')
        if gh is None or ga is None: continue
        is_home=x['teams']['home']['id']==team_id
        gf=gh if is_home else ga; gc=ga if is_home else gh
        out.append({'gf':gf,'ga':gc,'tot':gh+ga,'btts':gh>0 and ga>0,'role':'H' if is_home else 'A'})
    return out

def pct(n,d): return n/d if d else 0.0

def stats(rows, role):
    ov=rows[:5]
    rr=[r for r in rows if r['role']==role][:5]
    def pack(a):
        n=len(a)
        return {
            'n':n,
            'score':sum(r['gf']>0 for r in a),
            'btts':sum(r['btts'] for r in a),
            'two':sum(r['tot']>=2 for r in a),
            'low':sum(r['tot']<=1 for r in a),
            'avg_gf':sum(r['gf'] for r in a)/n if n else 0,
            'avg_ga':sum(r['ga'] for r in a)/n if n else 0,
            'avg_tot':sum(r['tot'] for r in a)/n if n else 0,
        }
    return pack(ov), pack(rr)

def poisson_p2(lam):
    return 1-math.exp(-lam)*(1+lam)

def main():
    if not KEY:
        print('ERROR | API_FOOTBALL_KEY missing'); return 2
    cfg=json.load(open(sys.argv[1] if len(sys.argv)>1 else 'config/btts_scan_targets.json', encoding='utf-8'))
    day=cfg['date']; targets=cfg['targets']
    fixtures=get('/fixtures', {'date':day})
    print(f'API_FALLBACK_BTTS | {day} | targets={len(targets)} api_fixtures={len(fixtures)}')
    matched=[]; missing=[]
    for home,away in targets:
        best=None; bs=0
        for f in fixtures:
            h=f['teams']['home']['name']; a=f['teams']['away']['name']
            s=(sim(home,h)+sim(away,a))/2
            if s>bs: best,bs=f,s
        if best is None or bs<0.62:
            missing.append((home,away,bs)); continue
        matched.append((home,away,best,bs))
    cache={}
    ranked=[]
    for i,(th,ta,f,ms) in enumerate(matched,1):
        hid=f['teams']['home']['id']; aid=f['teams']['away']['id']
        try:
            if hid not in cache: cache[hid]=finished_rows(hid,20); time.sleep(.08)
            if aid not in cache: cache[aid]=finished_rows(aid,20); time.sleep(.08)
            hov,hrole=stats(cache[hid],'H'); aov,arole=stats(cache[aid],'A')
            # conservative scoring lambdas from attack/defence cross, clipped
            lh=max(.15,min(2.8,(hrole['avg_gf']+arole['avg_ga'])/2))
            la=max(.15,min(2.8,(arole['avg_gf']+hrole['avg_ga'])/2))
            lam=lh+la; p2=poisson_p2(lam); p00=math.exp(-lam)
            ph=1-math.exp(-lh); pa=1-math.exp(-la); pbtts=ph*pa
            empirical=(pct(hov['btts'],max(hov['n'],1))+pct(aov['btts'],max(aov['n'],1))+pct(hrole['btts'],max(hrole['n'],1))+pct(arole['btts'],max(arole['n'],1)))/4
            scoreprob=(pbtts*.55+empirical*.45)
            enough=min(hrole['n'],arole['n'])>=4 and min(hov['n'],aov['n'])>=5
            B=(enough and lam>=3.05 and p2>=.81 and min(lh,la)>=1.15 and hrole['two']>=4 and arole['two']>=4 and hov['two']==5 and aov['two']==5 and hrole['low']==0 and arole['low']==0 and hov['low']==0 and aov['low']==0 and hov['score']>=4 and aov['score']>=4 and scoreprob>=.58)
            A=(B and lam>=3.35 and p2>=.85 and min(lh,la)>=1.30 and hrole['two']==5 and arole['two']==5 and hrole['score']==5 and arole['score']==5 and hov['score']==5 and aov['score']==5 and scoreprob>=.63 and p00<=.04)
            tier='A' if A else 'B' if B else 'X'
            risk=(100*p00)
            rank=(100 if A else 60 if B else 0)+scoreprob*60+p2*25-min(30,risk*2)
            ranked.append((tier,rank,scoreprob,p2,p00,lam,min(lh,la),th,ta,ms,hov,hrole,aov,arole))
        except Exception as e:
            print(f'ERROR | {th} vs {ta} | {e}')
    ranked.sort(key=lambda x:x[1], reverse=True)
    for x in ranked:
        tier,rank,pb,p2,p00,lam,wlam,h,a,ms,hov,hr,aov,ar=x
        print(f'{tier} | {h} vs {a} | rank={rank:.2f} btts={pb:.3f} p2={p2:.3f} p00={p00:.3f} lambda={lam:.2f} weakLambda={wlam:.2f} Hov2={hov["two"]}/5 Hrole2={hr["two"]}/{hr["n"]} Aov2={aov["two"]}/5 Arole2={ar["two"]}/{ar["n"]} Hscore={hov["score"]}/5 Ascore={aov["score"]}/5 match={ms:.2f}')
    for h,a,s in missing: print(f'MISSING | {h} vs {a} | best={s:.2f}')
    print(f'SUMMARY | matched={len(matched)} missing={len(missing)} ranked={len(ranked)} A={sum(x[0]=="A" for x in ranked)} B={sum(x[0]=="B" for x in ranked)} X={sum(x[0]=="X" for x in ranked)}')
    return 0

if __name__=='__main__': raise SystemExit(main())
