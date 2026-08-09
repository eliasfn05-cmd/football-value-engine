from scanner.odds import parse_quotes
from scanner.providers.odds_api_io import OddsApiIoProvider


def test_odds_api_io_normalizes_totals_and_btts():
    payload = {
        "bookmakers": {
            "Bet365": [
                {"name": "Totals", "odds": [{"max": 2.5, "over": "1.91", "under": "1.89"}]},
                {"name": "Both Teams To Score", "odds": [{"yes": "1.82", "no": "1.96"}]},
            ]
        }
    }
    bookmakers = OddsApiIoProvider._api_football_bookmakers(payload)
    transformed = [{"bookmakers": bookmakers}]
    quotes = parse_quotes(transformed, preferred_bookmaker="Betano")

    assert quotes["over25"] is not None
    assert quotes["btts"] is not None
    assert quotes["over25"].decimal_odds == 1.91
    assert quotes["btts"].decimal_odds == 1.82
    assert quotes["over25"].bookmaker == "Bet365"


def test_parser_fills_missing_market_from_another_bookmaker():
    payload = [
        {
            "bookmakers": [
                {
                    "name": "Betano",
                    "bets": [
                        {"name": "Goals Over/Under", "values": [{"value": "Over 2.5", "odd": "1.75"}]}
                    ],
                },
                {
                    "name": "Bet365",
                    "bets": [
                        {"name": "Both Teams To Score", "values": [{"value": "Yes", "odd": "1.80"}]}
                    ],
                },
            ]
        }
    ]

    quotes = parse_quotes(payload, preferred_bookmaker="Betano")
    assert quotes["over25"].bookmaker == "Betano"
    assert quotes["btts"].bookmaker == "Bet365"
