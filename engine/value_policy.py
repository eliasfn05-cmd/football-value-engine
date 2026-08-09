from __future__ import annotations

from decimal import Decimal
from typing import Any


PREMIUM_VALUE_MIN_ODDS = Decimal("1.60")
PREMIUM_VALUE_MAX_ODDS = Decimal("2.40")
PREMIUM_SAFE_MIN_ODDS = Decimal("1.30")
PREMIUM_MIN_EV = Decimal("0.03")
TIER_A_MIN_EV = Decimal("0.08")
TIER_B_MIN_EV = PREMIUM_MIN_EV


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def odds_band(odds: Any) -> str:
    value = _decimal(odds)
    if value is None:
        return "MISSING"
    if PREMIUM_VALUE_MIN_ODDS <= value <= PREMIUM_VALUE_MAX_ODDS:
        return "PREMIUM_VALUE"
    if PREMIUM_SAFE_MIN_ODDS <= value < PREMIUM_VALUE_MIN_ODDS:
        return "PREMIUM_SAFE"
    return "OUTSIDE"


def is_premium_value_odds(odds: Any) -> bool:
    return odds_band(odds) == "PREMIUM_VALUE"


def is_premium_safe_odds(odds: Any) -> bool:
    return odds_band(odds) == "PREMIUM_SAFE"


def has_minimum_value(expected_value: Any) -> bool:
    value = _decimal(expected_value)
    return value is not None and value >= PREMIUM_MIN_EV
