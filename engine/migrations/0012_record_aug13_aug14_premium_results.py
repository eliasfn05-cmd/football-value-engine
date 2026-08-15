from decimal import Decimal

from django.db import migrations
from django.utils import timezone


AUG13 = "2026-08-13"
AUG14 = "2026-08-14"


def _norm(value):
    return (value or "").strip().lower()


def _matches(fixture, home_aliases, away_aliases):
    home = _norm(fixture.home_team.name)
    away = _norm(fixture.away_team.name)
    direct = any(alias in home for alias in home_aliases) and any(
        alias in away for alias in away_aliases
    )
    reverse = any(alias in away for alias in home_aliases) and any(
        alias in home for alias in away_aliases
    )
    return direct or reverse


def _ensure_ledger_from_daily(apps, target_date, home_aliases, away_aliases):
    DailyPremiumSelection = apps.get_model("engine", "DailyPremiumSelection")
    PremiumPublicationLedger = apps.get_model("engine", "PremiumPublicationLedger")

    existing = PremiumPublicationLedger.objects.select_related(
        "prediction",
        "prediction__fixture",
        "prediction__fixture__home_team",
        "prediction__fixture__away_team",
    ).filter(target_date=target_date)
    for ledger in existing:
        if _matches(ledger.prediction.fixture, home_aliases, away_aliases):
            return ledger

    rows = DailyPremiumSelection.objects.select_related(
        "prediction",
        "prediction__fixture",
        "prediction__fixture__home_team",
        "prediction__fixture__away_team",
    ).filter(target_date=target_date).order_by("rank")

    for row in rows:
        prediction = row.prediction
        fixture = prediction.fixture
        if not _matches(fixture, home_aliases, away_aliases):
            continue

        snapshot = {
            "fixture_id": fixture.id,
            "fixture_external_id": fixture.external_id,
            "home_team": fixture.home_team.name,
            "away_team": fixture.away_team.name,
            "kickoff": fixture.kickoff.isoformat(),
            "fixture_status_at_publication": fixture.status,
            "market": prediction.market,
            "selection": prediction.selection,
            "odds": float(prediction.market_odds) if prediction.market_odds is not None else None,
            "score": float(prediction.score or 0),
            "premium_tier": row.premium_tier,
            "premium_rank_score": float(row.premium_rank_score),
            "rationale": row.rationale or {},
            "historical_backfill": "requested_2026_08_14",
        }
        ledger, _ = PremiumPublicationLedger.objects.get_or_create(
            prediction_id=prediction.id,
            defaults={
                "target_date": target_date,
                "published_rank": row.rank,
                "premium_tier": row.premium_tier,
                "premium_rank_score": row.premium_rank_score,
                "model_version": row.model_version,
                "market": prediction.market,
                "selection": prediction.selection,
                "odds": prediction.market_odds,
                "snapshot": snapshot,
            },
        )
        return ledger
    return None


def _settle(PredictionOutcome, ledger, result, reason, home_goals=None, away_goals=None):
    if ledger is None:
        return

    odds = Decimal(str(ledger.odds or "1.00"))
    if result == "WIN":
        profit = odds - Decimal("1.0000")
    elif result == "LOSS":
        profit = Decimal("-1.0000")
    else:
        profit = Decimal("0.0000")

    # Prefer an already-ingested final score. Only use explicit fallback scores
    # when the user supplied them (Rangers-Jagiellonia 1-1).
    fixture = ledger.prediction.fixture
    hg = fixture.home_goals if fixture.home_goals is not None else home_goals
    ag = fixture.away_goals if fixture.away_goals is not None else away_goals

    PredictionOutcome.objects.update_or_create(
        prediction_id=ledger.prediction_id,
        defaults={
            "result": result,
            "home_goals": hg,
            "away_goals": ag,
            "stake_units": Decimal("1.000"),
            "profit_units": profit,
            "settled_at": timezone.now(),
            "settlement_reason": reason,
        },
    )

    if home_goals is not None and away_goals is not None:
        fixture.home_goals = home_goals
        fixture.away_goals = away_goals
        fixture.status = "FT"
        fixture.save(update_fields=["home_goals", "away_goals", "status"])


def record_aug13_aug14_results(apps, schema_editor):
    PredictionOutcome = apps.get_model("backtesting", "PredictionOutcome")

    # 13-Aug: official market result. Rangers-Jagiellonia was an Over 2.5 LOSS
    # at 1-1 even though BTTS was true; keeping the official market outcome
    # avoids inflating Premium hit rate. 1 de Marzo-Tembetary Over 2.5 was WIN.
    rangers = _ensure_ledger_from_daily(
        apps, AUG13, ("rangers",), ("jagiell",)
    )
    _settle(
        PredictionOutcome,
        rangers,
        "LOSS",
        "user_confirmed_aug13_rangers_jagiellonia_over25",
        home_goals=1,
        away_goals=1,
    )

    marzo = _ensure_ledger_from_daily(
        apps,
        AUG13,
        ("1 de marzo", "1° de marzo", "1º de marzo"),
        ("tembetary",),
    )
    _settle(
        PredictionOutcome,
        marzo,
        "WIN",
        "user_confirmed_aug13_1_de_marzo_tembetary_over25",
    )

    # 14-Aug: all three Premium Over 2.5 selections were confirmed as WIN.
    # Exact scores are intentionally not fabricated; if ingestion already has
    # them they are preserved, otherwise outcome rows remain score-null until
    # the normal results feed fills the fixture.
    aug14_cases = (
        (("correcaminos",), ("tapat",), "user_confirmed_aug14_correcaminos_tapatio_over25"),
        (("shamiya",), ("sporty",), "user_confirmed_aug14_shamiya_sporty_over25"),
        (("brage",), ("orebro", "örebro"), "user_confirmed_aug14_brage_orebro_over25"),
    )
    for home_aliases, away_aliases, reason in aug14_cases:
        ledger = _ensure_ledger_from_daily(apps, AUG14, home_aliases, away_aliases)
        _settle(PredictionOutcome, ledger, "WIN", reason)


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("engine", "0011_remove_reims_dunkerque_premium_lock"),
        ("backtesting", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(record_aug13_aug14_results, reverse_noop),
    ]
