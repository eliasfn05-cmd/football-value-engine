from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db.models import Q

from engine.btts_v25_policy import anti_zero_metrics
from engine.btts_v291_policy import tier_a_decision_v291
from engine.models import Fixture, Prediction


def _blocked(d):
    return bool(d and getattr(d, "blocked", False))


def _team_previous(team, fixture, role=None, limit=10):
    filters = dict(kickoff__lt=fixture.kickoff, home_goals__isnull=False, away_goals__isnull=False)
    if role == "home":
        qs = Fixture.objects.filter(home_team=team, **filters)
    elif role == "away":
        qs = Fixture.objects.filter(away_team=team, **filters)
    else:
        qs = Fixture.objects.filter(Q(home_team=team) | Q(away_team=team), **filters)
    return list(qs.order_by("-kickoff")[:limit])


def _def_profile(team, fixture, role=None):
    games = _team_previous(team, fixture, role=role, limit=10)
    if not games:
        return None
    conceded, clean = [], []
    for g in games:
        if g.home_team_id == team.id:
            ga = int(g.away_goals or 0)
        else:
            ga = int(g.home_goals or 0)
        conceded.append(ga)
        clean.append(int(ga == 0))
    n = len(games)
    return {
        "n": n,
        "clean_sheet_rate": sum(clean) / n,
        "last5_clean_sheets": sum(clean[:5]),
        "last10_clean_sheets": sum(clean),
        "avg_ga": sum(conceded) / n,
    }


def _snapshot(p):
    m = anti_zero_metrics(p)
    if not m.get("available"):
        return None
    f = p.fixture
    home_def = _def_profile(f.home_team, f, "home")
    away_def = _def_profile(f.away_team, f, "away")
    home_all = _def_profile(f.home_team, f)
    away_all = _def_profile(f.away_team, f)
    if not all((home_def, away_def, home_all, away_all)):
        return None
    # For BTTS, a strong clean-sheet opponent is a direct threat to the other side scoring.
    max_role_cs = max(home_def["clean_sheet_rate"], away_def["clean_sheet_rate"])
    max_all_cs = max(home_all["clean_sheet_rate"], away_all["clean_sheet_rate"])
    max_last5_cs = max(home_def["last5_clean_sheets"], away_def["last5_clean_sheets"])
    return {
        "max_role_cs": max_role_cs,
        "max_all_cs": max_all_cs,
        "max_last5_cs": max_last5_cs,
        "home_role_cs": home_def["clean_sheet_rate"],
        "away_role_cs": away_def["clean_sheet_rate"],
        "home_role_ga": home_def["avg_ga"],
        "away_role_ga": away_def["avg_ga"],
        "emp": float(m.get("empirical_btts") or 0),
        "cons": float(m.get("consensus_probability") or 0),
    }


class Command(BaseCommand):
    help = "Audit-only: valida si la fortaleza defensiva/clean sheets del rival separa losses BTTS Premium de wins, sin leakage."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10000)
        parser.add_argument("--show", type=int, default=50)

    def handle(self, *args, **o):
        limit = max(100, min(o["limit"], 10000))
        show = max(1, min(o["show"], 100))
        qs = Prediction.objects.filter(
            market__iexact="BTTS", fixture__home_goals__isnull=False, fixture__away_goals__isnull=False
        ).select_related("fixture", "fixture__home_team", "fixture__away_team").order_by("-fixture__kickoff", "-created_at")[:limit]
        seen, rows = set(), []
        for p in qs:
            if p.fixture_id in seen:
                continue
            seen.add(p.fixture_id)
            if _blocked(tier_a_decision_v291(p)):
                continue
            m = _snapshot(p)
            if not m:
                continue
            f = p.fixture
            won = f.home_goals > 0 and f.away_goals > 0
            rows.append((p, m, won, (f.home_goals == 0) != (f.away_goals == 0), f.home_goals == 0 and f.away_goals == 0))

        self.stdout.write(self.style.SUCCESS(f"BTTS V2.9.5 OPPONENT CLEAN-SHEET RISK AUDIT | reconstructed={len(rows)}"))
        self.stdout.write("SOURCE=PRE_KICKOFF_ONLY | audit-only; NO production gate changes.")
        flags = [
            ("role_cs>=0.30", lambda m: m["max_role_cs"] >= .30),
            ("role_cs>=0.40", lambda m: m["max_role_cs"] >= .40),
            ("overall_cs>=0.30", lambda m: m["max_all_cs"] >= .30),
            ("last5_role_cs>=2", lambda m: m["max_last5_cs"] >= 2),
            ("role_cs>=.30 & emp<.68", lambda m: m["max_role_cs"] >= .30 and m["emp"] < .68),
        ]
        wins = [r for r in rows if r[2]]
        losses = [r for r in rows if not r[2]]
        self.stdout.write(f"wins={len(wins)} losses={len(losses)} one_sided={sum(r[3] for r in rows)} zero_zero={sum(r[4] for r in rows)}")
        self.stdout.write("\nCANDIDATE FLAGS")
        for name, fn in flags:
            wf = sum(fn(r[1]) for r in wins); lf = sum(fn(r[1]) for r in losses)
            wr = wf / len(wins) if wins else 0; lr = lf / len(losses) if losses else 0
            self.stdout.write(f"{name:28s} WIN={wf}/{len(wins)}({wr:.2f}) LOSS={lf}/{len(losses)}({lr:.2f}) separation={lr-wr:+.2f}")
        self.stdout.write("\nLOSS DETAILS")
        for p, m, won, one, zz in losses[:show]:
            f=p.fixture
            self.stdout.write(f"{'ONE' if one else '0-0' if zz else 'LOSS'} | {f.home_goals}-{f.away_goals} | {f.home_team.name} vs {f.away_team.name} | homeCS={m['home_role_cs']:.2f} awayCS={m['away_role_cs']:.2f} maxCS={m['max_role_cs']:.2f} last5CS={m['max_last5_cs']:.0f} emp={m['emp']:.3f} cons={m['cons']:.3f}")
        self.stdout.write(self.style.WARNING("No promover ningun filtro desde este audit aislado. Si separa losses de wins, validarlo despues en walk-forward/holdout."))
