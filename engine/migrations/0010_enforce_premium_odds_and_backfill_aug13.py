from decimal import Decimal

from django.db import migrations


MIN_ODDS = Decimal("1.60")
MAX_ODDS = Decimal("2.40")
TARGET_DATE = "2026-08-13"


def _normalized(value):
    return (value or "").strip().lower()


def enforce_odds_and_backfill_aug13(apps, schema_editor):
    DailyPremiumSelection = apps.get_model("engine", "DailyPremiumSelection")
    PremiumPublicationLedger = apps.get_model("engine", "PremiumPublicationLedger")

    # Hard cleanup for the operational card. A pick outside 1.60-2.40 must not
    # survive into the next Premium reconcile, even if it was published before
    # the odds policy was enforced in the selector.
    invalid_daily_ids = []
    for row in DailyPremiumSelection.objects.select_related("prediction").all():
        odds = row.prediction.market_odds
        if odds is None or odds < MIN_ODDS or odds > MAX_ODDS:
            invalid_daily_ids.append(row.id)
    if invalid_daily_ids:
        DailyPremiumSelection.objects.filter(id__in=invalid_daily_ids).delete()

    # Remove only current/future invalid publication locks. Historical ledgers
    # remain immutable for audit purposes.
    invalid_publication_ids = []
    for row in PremiumPublicationLedger.objects.select_related("prediction").filter(
        target_date__gte="2026-08-14"
    ):
        current_odds = row.prediction.market_odds
        published_odds = row.odds
        if (
            published_odds is None
            or published_odds < MIN_ODDS
            or published_odds > MAX_ODDS
            or current_odds is None
            or current_odds < MIN_ODDS
            or current_odds > MAX_ODDS
        ):
            invalid_publication_ids.append(row.id)
    if invalid_publication_ids:
        PremiumPublicationLedger.objects.filter(id__in=invalid_publication_ids).delete()

    # Backfill the two Premium picks requested for 13-Aug from the exact daily
    # selections that the system produced. This preserves their real market,
    # odds, tier and rank instead of inventing values during the migration.
    requested_pairs = (
        (("ranger",), ("jagiell",)),
        (("1 de marzo", "1° de marzo", "1º de marzo"), ("tembetary",)),
    )

    rows = DailyPremiumSelection.objects.select_related(
        "prediction",
        "prediction__fixture",
        "prediction__fixture__home_team",
        "prediction__fixture__away_team",
    ).filter(target_date=TARGET_DATE).order_by("rank")

    for row in rows:
        prediction = row.prediction
        fixture = prediction.fixture
        home = _normalized(fixture.home_team.name)
        away = _normalized(fixture.away_team.name)

        matched = False
        for home_aliases, away_aliases in requested_pairs:
            direct = any(alias in home for alias in home_aliases) and any(
                alias in away for alias in away_aliases
            )
            reverse = any(alias in away for alias in home_aliases) and any(
                alias in home for alias in away_aliases
            )
            if direct or reverse:
                matched = True
                break
        if not matched or prediction.market_odds is None:
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
            "odds": float(prediction.market_odds),
            "score": float(prediction.score or 0),
            "premium_tier": row.premium_tier,
            "premium_rank_score": float(row.premium_rank_score),
            "rationale": row.rationale or {},
            "historical_backfill": "requested_2026_08_14",
        }
        PremiumPublicationLedger.objects.get_or_create(
            prediction_id=prediction.id,
            defaults={
                "target_date": TARGET_DATE,
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


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("engine", "0009_record_aug12_premium_results"),
    ]

    operations = [
        migrations.RunPython(enforce_odds_and_backfill_aug13, reverse_noop),
    ]
