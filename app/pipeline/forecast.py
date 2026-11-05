from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

MM_PER_INCH = 25.4
_DURATION = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def inches_from_qpf(value, unit: str | None) -> float | None:
    if value is None:
        return None
    amount = float(value)
    label = (unit or "").lower()
    if "in" in label and "mm" not in label:
        return amount
    return amount / MM_PER_INCH


def parse_duration(text: str) -> timedelta:
    match = _DURATION.fullmatch(text or "")
    if not match or text in {"P", "PT"}:
        raise ValueError(f"Unsupported duration '{text}'")
    days, hours, minutes, seconds = (int(part) if part else 0 for part in match.groups())
    duration = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    if duration.total_seconds() <= 0:
        raise ValueError(f"Unsupported duration '{text}'")
    return duration


def parse_valid_interval(spec: str) -> tuple[datetime, datetime]:
    start_text, duration_text = spec.split("/", 1)
    start = datetime.fromisoformat(start_text)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    duration = parse_duration(duration_text)
    return start, start + duration


def rolling_max_inches(
    buckets: list[tuple[datetime, datetime, float | None]],
    window_hours: int,
    horizon_hours: int,
    now: datetime,
) -> float | None:
    """Maximum sum of consecutive NWS QPF buckets that exactly fill the window.

    Buckets shorter than the requested window are summed. A window shorter than
    the forecast step cannot be resolved and returns None rather than a scaled guess.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    horizon_end = now + timedelta(hours=horizon_hours)
    usable = []
    for start, end, value in sorted(buckets, key=lambda item: item[0]):
        if end <= now or start >= horizon_end:
            continue
        usable.append((start, end, None if value is None else float(value)))
    if not usable:
        return None
    window = timedelta(hours=window_hours)
    smallest = min(end - start for start, end, _ in usable)
    if window + timedelta(minutes=1) < smallest:
        return None

    best = None
    tolerance = timedelta(minutes=1)
    for index, _bucket in enumerate(usable):
        total = 0.0
        duration = timedelta(0)
        complete = False
        for start, end, value in usable[index:]:
            if value is None:
                break
            step = end - start
            if duration > timedelta(0) and duration + step > window + tolerance:
                break
            total += value
            duration += step
            if abs(duration - window) <= tolerance:
                complete = True
                break
            if duration > window + tolerance:
                break
        if complete:
            best = total if best is None else max(best, total)
    return best
