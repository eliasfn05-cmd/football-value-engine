from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand

from engine.btts_v291_policy import anti_zero_decision_v291, tier_a_decision_v291
from engine.btts_v292_policy import anti_zero_decision_v292, tier_a_decision_v292
from engine.models import Prediction


def _blocked(decision) -> bool:
    return bool(decision and getattr(decision, "blocked", False))


def _evaluate(predictions, decision_fn):
    picks = wins = losses = zero_zero_losses = one_sided_losses = 0
    staked = Decimal("0")
    returned = Decimal("0")
    reasons: dict[str, int] = {}

    for p in predictions:
        decision = decision_fn(p)
        if _blocked(decision):
            code = getattr(decision, "code", "blocked")
            reasons[code] = reasons.get(code, 0) + 1
            continue

        fixture = p.fixture
        if fixture.home_goals is None or fixture.away_goals is None:
            continue

        picks += 1
        btts = fixture.home_goals > 0 and fixture.away_goals > 0
        if btts:
            wins += 1
        else:
            losses += 1
            if fixture.home_goals == 0 and fixture.away_goals == 0:
                zero_zero_losses += 1
            else:
                one_sided_losses += 1

        if p.market_odds is not None and p.market_odds > 0:
            staked += Decimal("1")
            if btts:
                returned += Decimal(p.market_odds)

    roi = None
    if staked > 0:
        roi = float((returned - staked) / staked)

    return {
        "picks": picks,
        "wins": wins,
        "losses": losses,
        "hit_rate": round(wins / picks, 4) if picks else None,
        "zero_zero_losses": zero_zero_losses,
        "one_sided_losses": one_sided_losses,
        "roi_flat_1u": round(roi, 4) if roi is not None else None,
        "priced_picks": int(staked),
        "top_rejection_reasons": sorted(
            reasons.items(), key=lambda item: item[1], reverse=True
        )[:15],
    }


def _delta(old, new, key):
    a, b = old.get(key), new.get(key)
    if a is None or b is None:
        return None
    return round(b - a, 4)


def _removed_audit(predictions, old_fn, new_fn):
    removed = []
    for p in predictions:
        old_block = _blocked(old_fn(p))
        new_decision = new_fn(p)
        new_block = _blocked(new_decision)
        if old_block or not new_block:
            continue
        fixture = p.fixture
        if fixture.home_goals is None or fixture.away_goals is None:
            continue
        won = fixture.home_goals > 0 and fixture.away_goals > 0
        removed.append(
            (
                won,
                getattr(new_decision, "code", "blocked"),
                f"{fixture.home_team.name} vs {fixture.away_team.name}",
                f"{fixture.home_goals}-{fixture.away_goals}",
            )
        )
    return removed


class Command(BaseCommand):
    help = (
        "Ejecuta un walk-forward comparativo real BTTS V2.9.1 vs V2.9.2 "
        "sobre fixtures finalizados usando solo evidencia previa al kickoff."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=10000,
            help="Maximo de snapshots BTTS recientes a considerar (50-10000).",
        )
        parser.add_argument(
            "--show-removed",
            type=int,
            default=20,
            help="Cantidad de picks V2.9.1 removidos por V2.9.2 a mostrar.",
        )

    def handle(self, *args, **options):
        limit = max(50, min(int(options["limit"]), 10000))
        show_removed = max(0, min(int(options["show_removed"]), 100))

        qs = (
            Prediction.objects.filter(
                market__iexact="BTTS",
                fixture__home_goals__isnull=False,
                fixture__away_goals__isnull=False,
            )
            .select_related("fixture", "fixture__home_team", "fixture__away_team")
            .order_by("-fixture__kickoff", "-created_at")[:limit]
        )

        unique = []
        seen = set()
        for prediction in qs:
            if prediction.fixture_id in seen:
                continue
            seen.add(prediction.fixture_id)
            unique.append(prediction.pk)

        base = list(
            Prediction.objects.filter(pk__in=unique)
            .select_related("fixture", "fixture__home_team", "fixture__away_team")
            .order_by("fixture__kickoff")
        )

        v291 = _evaluate(base, anti_zero_decision_v291)
        v292 = _evaluate(base, anti_zero_decision_v292)
        v291_a = _evaluate(base, tier_a_decision_v291)
        v292_a = _evaluate(base, tier_a_decision_v292)

        self.stdout.write(
            self.style.SUCCESS(
                f"BTTS V2.9.1 vs V2.9.2 | fixtures unicos evaluados: {len(base)}"
            )
        )
        self.stdout.write(
            "Metodo: walk-forward policy replay; cada politica usa solo informacion previa al kickoff."
        )

        self._print_block("GENERIC", v291, v292)
        self._print_block("TIER A", v291_a, v292_a)

        if show_removed:
            self._print_removed(
                "REMOVIDOS GENERIC POR V2.9.2",
                _removed_audit(base, anti_zero_decision_v291, anti_zero_decision_v292),
                show_removed,
            )
            self._print_removed(
                "REMOVIDOS TIER A POR V2.9.2",
                _removed_audit(base, tier_a_decision_v291, tier_a_decision_v292),
                show_removed,
            )

    def _print_block(self, label, old, new):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(label))
        self.stdout.write(
            "V2.9.1 -> picks={picks} wins={wins} losses={losses} hit={hit} "
            "roi={roi} 0-0={zz} one-sided={os} priced={priced}".format(
                picks=old["picks"], wins=old["wins"], losses=old["losses"],
                hit=old["hit_rate"], roi=old["roi_flat_1u"],
                zz=old["zero_zero_losses"], os=old["one_sided_losses"],
                priced=old["priced_picks"],
            )
        )
        self.stdout.write(
            "V2.9.2 -> picks={picks} wins={wins} losses={losses} hit={hit} "
            "roi={roi} 0-0={zz} one-sided={os} priced={priced}".format(
                picks=new["picks"], wins=new["wins"], losses=new["losses"],
                hit=new["hit_rate"], roi=new["roi_flat_1u"],
                zz=new["zero_zero_losses"], os=new["one_sided_losses"],
                priced=new["priced_picks"],
            )
        )
        self.stdout.write(
            "DELTA  -> picks={picks:+d} wins={wins:+d} losses={losses:+d} "
            "hit={hit} roi={roi} 0-0={zz:+d} one-sided={os:+d}".format(
                picks=new["picks"] - old["picks"],
                wins=new["wins"] - old["wins"],
                losses=new["losses"] - old["losses"],
                hit=_delta(old, new, "hit_rate"),
                roi=_delta(old, new, "roi_flat_1u"),
                zz=new["zero_zero_losses"] - old["zero_zero_losses"],
                os=new["one_sided_losses"] - old["one_sided_losses"],
            )
        )
        self.stdout.write(f"V2.9.2 top rechazos: {new['top_rejection_reasons']}")

    def _print_removed(self, label, rows, limit):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(label))
        wins_removed = sum(1 for won, *_ in rows if won)
        losses_removed = len(rows) - wins_removed
        self.stdout.write(
            f"total={len(rows)} winners_rechazados={wins_removed} losses_evitados={losses_removed}"
        )
        for won, reason, match, score in rows[:limit]:
            status = "WIN_REMOVED" if won else "LOSS_AVOIDED"
            self.stdout.write(f"{status} | {score} | {match} | {reason}")
