from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta

from django.utils import timezone


DEFAULT_START_TIME = "00:00"
DEFAULT_END_TIME = "23:59"


def normalize_clock(value: str | None, *, default: str) -> str:
    raw = str(value or default).strip()
    try:
        parsed = datetime.strptime(raw, "%H:%M").time()
    except ValueError as exc:
        raise ValueError(f"Invalid time '{raw}'. Expected HH:MM") from exc
    return parsed.strftime("%H:%M")


def window_bounds(
    target_date: date,
    *,
    start_time: str | None = None,
    end_time: str | None = None,
):
    """Return an inclusive-minute kickoff window as [start, end_exclusive).

    The dashboard/workflow can supply explicit values. Management commands that
    run inside the same GitHub Actions job inherit PREMIUM_WINDOW_START/END, so
    every expensive stage sees the same fixture subset without changing any
    scoring, calibration or Premium admission rule.
    """
    start_raw = start_time if start_time is not None else os.getenv("PREMIUM_WINDOW_START")
    end_raw = end_time if end_time is not None else os.getenv("PREMIUM_WINDOW_END")
    start_text = normalize_clock(start_raw, default=DEFAULT_START_TIME)
    end_text = normalize_clock(end_raw, default=DEFAULT_END_TIME)

    start_clock = datetime.strptime(start_text, "%H:%M").time()
    end_clock = datetime.strptime(end_text, "%H:%M").time()
    start = timezone.make_aware(datetime.combine(target_date, start_clock))
    end_inclusive = timezone.make_aware(datetime.combine(target_date, end_clock))
    if end_inclusive < start:
        raise ValueError("End time must be equal to or later than start time")

    # Inputs have minute precision and the UI treats Hora fin as inclusive.
    return start, end_inclusive + timedelta(minutes=1)


def window_label(start_time: str | None = None, end_time: str | None = None) -> str:
    start_raw = start_time if start_time is not None else os.getenv("PREMIUM_WINDOW_START")
    end_raw = end_time if end_time is not None else os.getenv("PREMIUM_WINDOW_END")
    return (
        f"{normalize_clock(start_raw, default=DEFAULT_START_TIME)}-"
        f"{normalize_clock(end_raw, default=DEFAULT_END_TIME)}"
    )
