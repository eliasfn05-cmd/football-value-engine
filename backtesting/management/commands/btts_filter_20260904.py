from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from django.core.management.base import BaseCommand

from engine.btts_v25_policy import anti_zero_metrics
from engine.btts_v291_policy import tier_a_decision_v291
from engine.btts_v299_policy import tier_a_decision_v299, anti_zero_decision_v299
from engine.models import Fixture, Prediction

DAY = "2026-09-04"
TARGETS = [
    ("Chornomorets Odesa", "Livyi Bereh"),
    ("Bali United", "PSS Sleman"),
    ("Pattani", "Pathum United"),
    ("Uttaradit", "Muangthong"),
    ("Ittihad Kalba", "Al Ain"),
    ("Lucko", "Solin"),
    ("Uljanik Pula", "Dugo Selo"),
    ("Polissya Zhytomyr", "Kudrivka"),
    ("Hobro", "Hvidovre IF"),
    ("Vejle", "Vendsyssel"),
    ("Liptovsky Mikulas", "Inter Bratislava"),
    ("Grodzisk Mazowiecki", "Warta Poznan"),
    ("Stalowa Wola", "Zawisza"),
    ("Al Wasl", "Khorfakkan"),
    ("Arminia Bielefeld", "St Pauli"),
    ("Hannover", "Karlsruher"),
    ("Vasas", "Nyiregyhaza"),
    ("Ingolstadt", "Aachen"),
    ("Viborg", "Lyngby"),
    ("Koge", "Hillerod"),
    ("Lyon", "Auxerre"),
    ("Aalesund", "Start"),
    ("Fredrikstad", "Bodo Glimt"),
    ("Novi Pazar", "Zeleznicar Pancevo"),
    ("Norrkoping", "Ljungskile"),
    ("Oster", "Nordic United"),
    ("Basaksehir", "Galatasaray"),
    ("Independiente FBC", "Fernando de la Mora"),
    ("Aarau", "Rapperswil Jona"),
    ("Kriens", "Lausanne Ouchy"),
    ("Varazdin", "Istra 1961"),
    ("Sparta Rotterdam", "Zwolle"),
    ("FC Cajamarca", "Cienciano"),
    ("Ind Juniors", "Cuenca Juniors"),
    ("Nafta", "Radomlje"),
    ("Etoile Carouge", "Wil"),
    ("Winterthur", "Xamax"),
    ("Stuttgart", "Koln"),
    ("Livingston", "Stenhousemuir"),
    ("Las Palmas", "Leganes"),
    ("Piast", "Katowice"),
    ("Lommel SK", "Club Brugge"),
    ("Briton Ferry", "Airbus"),
    ("Caernarfon", "Flint"),
    ("Cardiff Metropolitan", "Cambrian United"),
    ("Connahs Quay", "Haverfordwest"),
    ("Holywell", "Trefelin"),
    ("Llandudno", "Ammanford"),
    ("Bray", "Cork City"),
    ("Cobh Ramblers", "UC Dublin"),
    ("Finn Harps", "Athlone"),
    ("Treaty United", "Wexford"),
    ("Drogheda", "Galway"),
    ("Waterford", "Sligo Rovers"),
    ("Genoa", "Como"),
    ("Real Betis", "Real Madrid"),
    ("Ipswich", "Liverpool"),
    ("Shamrock Rovers", "Shelbourne"),
    ("Sandefjord", "Viking"),
    ("Termalica", "Lechia"),
    ("Santarem", "Caldas"),
    ("La Luz", "Huracan FC"),
    ("PSG", "Monaco"),
    ("Porto", "Moreirense"),
    ("Sportivo Carapegua", "Paraguari"),
    ("Alianza Atletico", "UTC"),
    ("Leones", "Atletico FC"),
    ("Atletico FC", "Nueve de Octubre"),
    ("22 de Julio", "Gualaceo"),
]


def norm(value):
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii").lower()
    value = re.sub(r"\b(fc|cf|if|club|sc|fk|ac|deportivo|independiente)\b", " ", value)
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def sim(a, b):
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def decision_text(decision):
    if decision is None:
        return "PASS"
    return f"BLOCK:{getattr(decision, 'code', 'unknown')}"


class Command(BaseCommand):
    help = "Read-only system filter for user screenshot fixtures on 2026-09-04."

    def handle(self, *args, **options):
        fixtures = list(Fixture.objects.filter(kickoff__date=DAY).select_related("home_team", "away_team"))
        self.stdout.write(f"BTTS SYSTEM FILTER | {DAY} | targets={len(TARGETS)} db_fixtures={len(fixtures)}")
        self.stdout.write("POLICY | production baseline V2.9.1 + V2.9.9 audit guard | pre-kickoff metrics only")
        rows = []
        for home, away in TARGETS:
            scored = sorted(
                [((sim(home, f.home_team.name) + sim(away, f.away_team.name)) / 2.0, f) for f in fixtures],
                key=lambda x: x[0], reverse=True,
            )
            best, fixture = scored[0] if scored else (0.0, None)
            if fixture is None or best < 0.70:
                self.stdout.write(f"MISSING | {home} vs {away} | best={best:.2f}")
                continue
            prediction = (
                Prediction.objects.filter(fixture=fixture, market__iexact="BTTS")
                .order_by("-created_at", "-id")
                .first()
            )
            if prediction is None:
                self.stdout.write(f"NO_PRED | {fixture.home_team.name} vs {fixture.away_team.name} | match={best:.2f}")
                continue
            metrics = anti_zero_metrics(prediction)
            d291 = tier_a_decision_v291(prediction)
            d299a = tier_a_decision_v299(prediction)
            d299b = anti_zero_decision_v299(prediction)
            available = bool(metrics.get("available"))
            weak = float(metrics.get("weakest_score_probability", 0.0) or 0.0)
            cal = float(metrics.get("calibrated_probability", 0.0) or 0.0)
            cons = float(metrics.get("consensus_probability", 0.0) or 0.0)
            emp = float(metrics.get("empirical_btts", 0.0) or 0.0)
            home_o = metrics.get("home_overall") or {}
            away_o = metrics.get("away_overall") or {}
            fts = max(float(home_o.get("failed_to_score_rate", 1.0) or 0.0), float(away_o.get("failed_to_score_rate", 1.0) or 0.0)) if available else 1.0
            l5s = min(int(home_o.get("last5_scored", 0) or 0), int(away_o.get("last5_scored", 0) or 0)) if available else 0
            odds = float(getattr(prediction, "market_odds", 0.0) or 0.0)
            tier = "A" if d291 is None and d299a is None else ("B" if d299b is None else "X")
            rank = (0 if tier == "X" else 100) + cons * 40 + weak * 30 + emp * 20 + cal * 10 - fts * 15
            rows.append((rank, tier, fixture, prediction, weak, cal, cons, emp, fts, l5s, odds, d291, d299a, d299b, best))

        rows.sort(key=lambda x: x[0], reverse=True)
        self.stdout.write("\nRANKED")
        for rank, tier, fixture, prediction, weak, cal, cons, emp, fts, l5s, odds, d291, d299a, d299b, match in rows:
            self.stdout.write(
                f"{tier} | {fixture.home_team.name} vs {fixture.away_team.name} | rank={rank:.2f} raw={float(prediction.score or 0):.1f} "
                f"weak={weak:.3f} cal={cal:.3f} cons={cons:.3f} emp={emp:.3f} maxFTS={fts:.3f} minL5scored={l5s}/5 odds={odds:.2f} "
                f"v291={decision_text(d291)} v299A={decision_text(d299a)} v299B={decision_text(d299b)} match={match:.2f}"
            )
