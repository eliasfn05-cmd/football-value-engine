from __future__ import annotations

from collections import Counter
from decimal import Decimal

from django.core.management.base import BaseCommand

from engine.btts_v27_policy import anti_zero_decision_v27
from engine.btts_v291_policy import anti_zero_decision_v291
from engine.models import Prediction


def _blocked(decision) -> bool:
    return bool(decision and getattr(decision, "blocked", False))


class Command(BaseCommand):
    help = "Audita picks aceptados por V2.9 pero rechazados por V2.9.1 sobre fixtures finalizados."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10000)
        parser.add_argument("--show", type=int, default=100)

    def handle(self, *args, **options):
        limit = max(50, min(int(options["limit"]), 10000))
        show = max(0, min(int(options["show"]), 500))

        qs = (
            Prediction.objects.filter(
                market__iexact="BTTS",
                fixture__home_goals__isnull=False,
                fixture__away_goals__isnull=False,
            )
            .select_related("fixture", "fixture__home_team", "fixture__away_team")
            .order_by("-fixture__kickoff", "-created_at")[:limit]
        )

        unique_pks, seen = [], set()
        for p in qs:
            if p.fixture_id in seen:
                continue
            seen.add(p.fixture_id)
            unique_pks.append(p.pk)

        base = list(
            Prediction.objects.filter(pk__in=unique_pks)
            .select_related("fixture", "fixture__home_team", "fixture__away_team")
            .order_by("fixture__kickoff")
        )

        rejected = []
        reasons = Counter()
        wins = losses = zero_zero = one_sided = priced = 0
        staked = returned = Decimal("0")

        for p in base:
            old = anti_zero_decision_v27(p)
            new = anti_zero_decision_v291(p)
            if _blocked(old) or not _blocked(new):
                continue

            f = p.fixture
            btts = f.home_goals > 0 and f.away_goals > 0
            code = getattr(new, "code", "blocked")
            reasons[code] += 1
            wins += int(btts)
            losses += int(not btts)
            zero_zero += int(not btts and f.home_goals == 0 and f.away_goals == 0)
            one_sided += int(not btts and not (f.home_goals == 0 and f.away_goals == 0))

            odds = p.market_odds
            if odds is not None and odds > 0:
                priced += 1
                staked += Decimal("1")
                if btts:
                    returned += Decimal(odds)

            rejected.append((p, code, btts))

        total = len(rejected)
        hit = wins / total if total else 0
        roi = float((returned - staked) / staked) if staked else None

        self.stdout.write(self.style.SUCCESS(
            f"REJECTED PICKS AUDIT V2.9 -> V2.9.1 | fixtures evaluados={len(base)}"
        ))
        self.stdout.write(
            f"Rechazados por V2.9.1 que V2.9 aceptaba: {total} | "
            f"wins={wins} losses={losses} hit={hit:.4f} 0-0={zero_zero} "
            f"one-sided={one_sided} priced={priced} roi={roi if roi is not None else 'N/A'}"
        )
        self.stdout.write(f"Razones: {reasons.most_common()}")

        if show:
            self.stdout.write("")
            self.stdout.write("DETALLE")
            for p, code, btts in rejected[-show:]:
                f = p.fixture
                odds = str(p.market_odds) if p.market_odds is not None else "N/A"
                self.stdout.write(
                    f"{f.kickoff:%Y-%m-%d} | {f.home_team} vs {f.away_team} | "
                    f"{f.home_goals}-{f.away_goals} | {'WIN' if btts else 'LOSS'} | "
                    f"odds={odds} | rejected_by={code}"
                )
