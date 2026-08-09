from __future__ import annotations

import os
from typing import Optional

from engine.quantitative import MarketQuote


def _normalize(text: str) -> str:
    return " ".join(str(text or "").strip().lower().replace("/", " ").split())


def _candidate_bookmakers(payload: list[dict]) -> list[dict]:
    bookmakers: list[dict] = []
    for response_item in payload:
        for bookmaker in response_item.get("bookmakers") or []:
            bookmakers.append(bookmaker)
    return bookmakers


def _preferred_bookmakers(payload: list[dict], preferred: str | None = None) -> list[dict]:
    preferred_name = _normalize(preferred or os.getenv("PREFERRED_BOOKMAKER", "Betano"))
    return [
        bookmaker
        for bookmaker in _candidate_bookmakers(payload)
        if _normalize(bookmaker.get("name", "")) == preferred_name
    ]


def _select_bookmaker(payload: list[dict], preferred: str | None = None) -> Optional[dict]:
    rows = _preferred_bookmakers(payload, preferred)
    return rows[0] if rows else None


def _quotes_from_bookmaker(bookmaker: dict | None) -> dict[str, MarketQuote | None]:
    if not bookmaker:
        return {"btts": None, "over25": None}

    bookmaker_name = bookmaker.get("name", "")
    btts_quote: MarketQuote | None = None
    over_quote: MarketQuote | None = None

    for bet in bookmaker.get("bets") or []:
        bet_name = _normalize(bet.get("name", ""))
        for value in bet.get("values") or []:
            selection = _normalize(value.get("value", ""))
            odd = value.get("odd")
            try:
                decimal = float(odd)
            except (TypeError, ValueError):
                continue

            if btts_quote is None and (
                "both teams" in bet_name or "both teams score" in bet_name
            ) and selection in {"yes", "sí", "si"}:
                btts_quote = MarketQuote(decimal_odds=decimal, bookmaker=bookmaker_name)

            if over_quote is None and (
                "goals over under" in bet_name or "over under" in bet_name or "total goals" in bet_name
            ):
                compact = selection.replace(" ", "")
                if compact in {"over2.5", "over2,5", "+2.5", "+2,5"}:
                    over_quote = MarketQuote(decimal_odds=decimal, bookmaker=bookmaker_name)

    return {"btts": btts_quote, "over25": over_quote}


def _merge_quotes(current: dict[str, MarketQuote | None], incoming: dict[str, MarketQuote | None]) -> dict[str, MarketQuote | None]:
    return {
        "btts": current.get("btts") or incoming.get("btts"),
        "over25": current.get("over25") or incoming.get("over25"),
    }


def _best_coverage_quotes(payload: list[dict], preferred_name: str) -> dict[str, MarketQuote | None]:
    best_quotes = {"btts": None, "over25": None}
    best_coverage = -1
    for bookmaker in _candidate_bookmakers(payload):
        if _normalize(bookmaker.get("name", "")) == preferred_name:
            continue
        quotes = _quotes_from_bookmaker(bookmaker)
        coverage = int(quotes["btts"] is not None) + int(quotes["over25"] is not None)
        if coverage > best_coverage:
            best_quotes = quotes
            best_coverage = coverage
    return best_quotes


def parse_quotes(
    payload: list[dict],
    preferred_bookmaker: str | None = None,
    *,
    allow_fallback: bool = False,
) -> dict[str, MarketQuote | None]:
    """Parse BTTS/Over 2.5 quotes with strict preferred-bookmaker default.

    Production enrichment opts into fallback explicitly. If the preferred book
    is absent, fallback chooses the bookmaker covering the most target markets.
    If the preferred book has only one target market, fallback fills only the
    missing market, preserving the preferred quote where it exists.
    """
    quotes = {"btts": None, "over25": None}
    preferred_rows = _preferred_bookmakers(payload, preferred_bookmaker)
    for bookmaker in preferred_rows:
        quotes = _merge_quotes(quotes, _quotes_from_bookmaker(bookmaker))
        if quotes["btts"] is not None and quotes["over25"] is not None:
            return quotes

    if not allow_fallback:
        return quotes

    preferred_name = _normalize(preferred_bookmaker or os.getenv("PREFERRED_BOOKMAKER", "Betano"))
    fallback_quotes = _best_coverage_quotes(payload, preferred_name)
    return _merge_quotes(quotes, fallback_quotes)
