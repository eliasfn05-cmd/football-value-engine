from django.db import migrations


# User-verified 90-minute BTTS results. These corrections are intentionally
# data-only: no model/gate/ranking parameters are changed here.
RESULTS = [
    ("Polessya", "Zorya Luhansk", 1, 2, "WIN"),
    ("Al Anwar", "Al-Ahli Jeddah", 1, 2, "WIN"),
    ("Stockholm Internationale", "Jarfalla", 2, 0, "LOSS"),
    ("Audax Italiano", "U. La Calera", 0, 0, "LOSS"),
    ("Deportivo Cuenca", "Mushuc Runa", 0, 0, "LOSS"),
    ("Leon", "Monterrey", 2, 0, "LOSS"),
    ("RKC Waalwijk", "Jong PSV", 2, 2, "WIN"),
    ("Winterthur", "Lausanne Ouchy", 1, 1, "WIN"),
]


def _norm(value):
    import unicodedata
    value = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(c for c in value if not unicodedata.combining(c)).lower().strip()


def _matches(actual, expected):
    a, e = _norm(actual), _norm(expected)
    aliases = {
        _norm("Jarfalla"): {_norm("Jarfalla"), _norm("Järfälla")},
        _norm("U. La Calera"): {_norm("U. La Calera"), _norm("Union La Calera"), _norm("Unión La Calera")},
        _norm("Leon"): {_norm("Leon"), _norm("León")},
        _norm("Lausanne Ouchy"): {_norm("Lausanne Ouchy"), _norm("Stade Lausanne Ouchy"), _norm("Lausanne-Ouchy")},
        _norm("Al-Ahli Jeddah"): {_norm("Al-Ahli Jeddah"), _norm("Al Ahli Jeddah"), _norm("Al Ahli")},
    }
    return a in aliases.get(e, {e})


def correct(apps, schema_editor):
    Ledger = apps.get_model("engine", "PremiumPublicationLedger")
    Outcome = apps.get_model("backtesting", "PredictionOutcome")
    Fixture = apps.get_model("engine", "Fixture")

    ledgers = list(
        Ledger.objects.filter(market__iexact="BTTS")
        .select_related("prediction__fixture__home_team", "prediction__fixture__away_team")
        .order_by("-published_at")
    )

    for home, away, hg, ag, result in RESULTS:
        ledger = next((row for row in ledgers
            if _matches(row.prediction.fixture.home_team.name, home)
            and _matches(row.prediction.fixture.away_team.name, away)), None)
        if ledger is None:
            # Preserve audit integrity: do not manufacture a Premium ledger row.
            # Still correct the fixture score when the fixture exists uniquely so
            # historical pre-kickoff reconstruction/backtests use truthful results.
            fixtures = [f for f in Fixture.objects.select_related("home_team", "away_team").all()
                        if _matches(f.home_team.name, home) and _matches(f.away_team.name, away)]
            if fixtures:
                f = sorted(fixtures, key=lambda x: x.kickoff, reverse=True)[0]
                f.home_goals, f.away_goals = hg, ag
                f.save(update_fields=["home_goals", "away_goals"])
            continue

        prediction = ledger.prediction
        fixture = prediction.fixture
        fixture.home_goals, fixture.away_goals = hg, ag
        fixture.save(update_fields=["home_goals", "away_goals"])

        odds = float(ledger.odds or prediction.market_odds or 0)
        won = result == "WIN"
        profit = (odds - 1.0) if won and odds > 0 else (-1.0 if not won else 0.0)
        Outcome.objects.update_or_create(
            prediction_id=ledger.prediction_id,
            defaults={
                "result": result,
                "home_goals": hg,
                "away_goals": ag,
                "stake_units": 1,
                "profit_units": profit,
                "settlement_reason": "manual_verified_correction_btts_20260829",
            },
        )


def reverse(apps, schema_editor):
    # Corrections reflect verified real-world scores and should not be reverted
    # automatically to previously known-bad values.
    pass


class Migration(migrations.Migration):
    dependencies = [("backtesting", "0009_settle_btts_comerciantes_supra_20260828")]
    operations = [migrations.RunPython(correct, reverse)]
