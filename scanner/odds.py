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


def _select_bookmaker(payload: list[dict], preferred: str | None = None) -> Optional[dict]:
    preferred = _normalize(preferred or os.getenv("PREFERRED_BOOKMAKER", "Betano"))
    bookmakers = _candidate_bookmakers(payload)
    for bookmaker in bookmakers:
        if _normalize(bookmaker.get("name", "")) == preferred:
            return bookmaker
    return None


def parse_quotes(payload: list[dict], preferred_bookmaker: str | None = None) -> dict[str, MarketQuote | None]:
    bookmaker = _select_bookmaker(payload, preferred_bookmaker)
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
