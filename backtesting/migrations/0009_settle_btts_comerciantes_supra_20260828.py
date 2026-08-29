from django.db import migrations


RESULTS = [
    {
        "home": "Comerciantes Unidos",
        "away": "FC Cajamarca",
        "home_goals": 2,
        "away_goals": 3,
        "result": "WIN",
    },
    {
        "home": "Supra du Quebec",
        "away": "Forge",
        "home_goals": 1,
        "away_goals": 0,
        "result": "LOSS",
    },
]


def _norm(value):
    import unicodedata
    value = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(c for c in value if not unicodedata.combining(c)).lower().strip()


def settle(apps, schema_editor):
    Ledger = apps.get_model("engine", "PremiumPublicationLedger")
    Outcome = apps.get_model("backtesting", "PredictionOutcome")

    ledgers = list(
        Ledger.objects.filter(market__iexact="BTTS")
        .select_related("prediction__fixture__home_team", "prediction__fixture__away_team")
        .order_by("-published_at")
    )

    for item in RESULTS:
        ledger = next(
            (
                row for row in ledgers
                if _norm(row.prediction.fixture.home_team.name) == _norm(item["home"])
                and _norm(row.prediction.fixture.away_team.name) in {_norm(item["away"]), _norm("Forge FC") if item["away"] == "Forge" else _norm(item["away"])}
            ),
            None,
        )
        if ledger is None:
            continue

        odds = float(ledger.odds or ledger.prediction.market_odds or 0)
        won = item["result"] == "WIN"
        profit = (odds - 1.0) if won and odds > 0 else (-1.0 if not won else 0.0)
        Outcome.objects.update_or_create(
            prediction_id=ledger.prediction_id,
            defaults={
                "result": item["result"],
                "home_goals": item["home_goals"],
                "away_goals": item["away_goals"],
                "stake_units": 1,
                "profit_units": profit,
                "settlement_reason": "manual_verified_btts_20260828",
            },
        )


def reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("backtesting", "0008_settle_btts_block_20260827")]
    operations = [migrations.RunPython(settle, reverse)]
