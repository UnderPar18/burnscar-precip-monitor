from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

import yaml
from dotenv import load_dotenv

from app.paths import CONFIG_PATH, ENV_PATH, SETTINGS_PATH

ALLOWED_HOURS = (1, 3, 6, 12, 24, 48, 72)
STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DC", "DE", "FL", "GA", "HI",
    "IA", "ID", "IL", "IN", "KS", "KY", "LA", "MA", "MD", "ME", "MI", "MN",
    "MO", "MS", "MT", "NC", "ND", "NE", "NH", "NJ", "NM", "NV", "NY", "OH",
    "OK", "OR", "PA", "PR", "RI", "SC", "SD", "TN", "TX", "UT", "VA", "VT",
    "WA", "WI", "WV", "WY",
}


@dataclass
class Settings:
    states: list[str]
    min_acres: float
    buffer_miles: float
    accum_hours: list[int]
    forecast_source: str
    forecast_hours: int
    wildfire_only: bool
    refresh_minutes: int
    highlight_inches: float
    highlight_hours: int

    def to_dict(self) -> dict:
        return asdict(self)


def load_environment() -> None:
    load_dotenv(ENV_PATH)


def load_settings() -> Settings:
    load_environment()
    raw = {}
    if CONFIG_PATH.exists():
        raw.update(yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {})
    if SETTINGS_PATH.exists():
        raw.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
    return settings_from_dict(raw)


def save_settings(settings: Settings) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings.to_dict(), indent=2), encoding="utf-8")


def parse_states(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [part for part in re.split(r"[\s,]+", value.strip().upper()) if part]
    else:
        parts = [str(part).strip().upper() for part in value if str(part).strip()]
    states = []
    for part in parts:
        code = part.removeprefix("US-")
        if code not in STATE_CODES:
            raise ValueError(f"Unknown state code '{part}'. Use abbreviations such as OR, CO.")
        if code not in states:
            states.append(code)
    return states


def settings_from_dict(raw: dict) -> Settings:
    states = parse_states(raw.get("states") or [])
    try:
        min_acres = float(raw.get("min_acres", 100))
        buffer_miles = float(raw.get("buffer_miles", 5))
        highlight_inches = float(raw.get("highlight_inches", 0.5))
        forecast_hours = int(raw.get("forecast_hours", 72))
        forecast_source = str(raw.get("forecast_source") or "nbm").strip().lower()
        refresh_minutes = int(raw.get("refresh_minutes", 15))
    except (TypeError, ValueError) as exc:
        raise ValueError("Acreage, buffer, highlight, and timing values must be numbers.") from exc

    hours_raw = raw.get("accum_hours") or [1, 6, 24, 48]
    if isinstance(hours_raw, str):
        hours_raw = [part for part in re.split(r"[\s,]+", hours_raw) if part]
    try:
        accum_hours = [int(hour) for hour in hours_raw]
    except (TypeError, ValueError) as exc:
        raise ValueError("Accumulation intervals must be whole hours.") from exc
    if not accum_hours:
        raise ValueError("Choose at least one accumulation interval.")
    invalid = [hour for hour in accum_hours if hour not in ALLOWED_HOURS]
    if invalid:
        raise ValueError(
            "Accumulation intervals must be one of "
            + ", ".join(str(hour) for hour in ALLOWED_HOURS)
            + f". Got {invalid}."
        )
    # Preserve the user's order while dropping duplicates.
    seen = set()
    ordered_hours = []
    for hour in accum_hours:
        if hour not in seen:
            ordered_hours.append(hour)
            seen.add(hour)

    if not 0 <= min_acres <= 5_000_000:
        raise ValueError("Minimum acreage must be between 0 and 5,000,000.")
    if not 0.1 <= buffer_miles <= 50:
        raise ValueError("Buffer distance must be between 0.1 and 50 miles.")
    if highlight_inches < 0 or highlight_inches > 50:
        raise ValueError("Highlight threshold must be between 0 and 50 inches.")
    if forecast_source not in ("nbm", "nws"):
        raise ValueError("Forecast source must be nbm or nws.")
    if forecast_hours not in (24, 48, 72, 120, 168):
        raise ValueError("Forecast horizon must be 24, 48, 72, 120, or 168 hours.")
    if not 5 <= refresh_minutes <= 180:
        raise ValueError("Refresh interval must be between 5 and 180 minutes.")

    highlight_hours = int(raw.get("highlight_hours") or ordered_hours[0])
    if highlight_hours not in ordered_hours:
        highlight_hours = ordered_hours[0]

    return Settings(
        states=states,
        min_acres=min_acres,
        buffer_miles=buffer_miles,
        accum_hours=ordered_hours,
        forecast_source=forecast_source,
        forecast_hours=forecast_hours,
        wildfire_only=bool(raw.get("wildfire_only", True)),
        refresh_minutes=refresh_minutes,
        highlight_inches=highlight_inches,
        highlight_hours=highlight_hours,
    )
