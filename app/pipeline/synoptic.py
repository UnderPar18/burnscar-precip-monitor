from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

import httpx
from shapely import Point, prepare

from app.net import USER_AGENT, redact

PRECIP_URL = "https://api.synopticdata.com/v2/stations/precip"


def station_precip(stations: list[dict], hours: list[int]) -> dict[str, float | None]:
    totals = {str(hour): [] for hour in hours}
    for station in stations:
        for hour in hours:
            value = station["precip"].get(str(hour))
            if value is not None:
                totals[str(hour)].append(value)
    return {
        hour: (max(values) if values else None)
        for hour, values in totals.items()
    }


def fetch_stations_in_buffer(buffer, hours: list[int], token: str) -> list[dict]:
    if not token:
        raise RuntimeError("SYNOPTIC_TOKEN is not set. Add it to .env and restart the app.")
    minx, miny, maxx, maxy = buffer.bounds
    params = {
        "bbox": f"{minx},{miny},{maxx},{maxy}",
        "pmode": "last",
        "accum_hours": ",".join(str(hour) for hour in hours),
        "units": "english",
        "obtimezone": "UTC",
        "token": token,
    }
    try:
        with httpx.Client(timeout=60, headers={"User-Agent": USER_AGENT}) as client:
            response = client.get(PRECIP_URL, params=params)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        raise RuntimeError(redact(str(exc))) from None
    summary = payload.get("SUMMARY") or {}
    message = str(summary.get("RESPONSE_MESSAGE") or "")
    if "no stations" in message.lower():
        return []
    if summary.get("RESPONSE_CODE") not in (1, None) and not payload.get("STATION"):
        raise RuntimeError(redact(message or "Synoptic precipitation request failed"))

    prepare(buffer)
    stations = []
    for raw in payload.get("STATION") or []:
        try:
            lon = float(raw["LONGITUDE"])
            lat = float(raw["LATITUDE"])
        except (KeyError, TypeError, ValueError):
            continue
        if not buffer.covers(Point(lon, lat)):
            continue
        precip = {}
        observations = (raw.get("OBSERVATIONS") or {}).get("precipitation") or []
        for item in observations:
            try:
                hour = str(int(item.get("accum_hours")))
            except (TypeError, ValueError):
                continue
            total = item.get("total")
            if total is None:
                continue
            precip[hour] = float(total)
        stations.append(
            {
                "stid": raw.get("STID"),
                "name": raw.get("NAME"),
                "lat": lat,
                "lon": lon,
                "precip": precip,
            }
        )
    return stations


def synoptic_token() -> str:
    return os.environ.get("SYNOPTIC_TOKEN", "").strip()


def hour_key(stamp: str) -> str | None:
    match = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2})", str(stamp))
    if not match:
        return None
    return f"{match.group(1)}T{match.group(2)}:00:00Z"


def hourly_totals(observations: list[dict]) -> list[dict]:
    """One precipitation total per clock hour, keeping the larger report."""
    by_hour: dict[str, float] = {}
    for item in observations:
        hour = hour_key(str(item.get("last_report") or ""))
        total = item.get("total")
        if hour is None or total is None:
            continue
        value = float(total)
        current = by_hour.get(hour)
        by_hour[hour] = value if current is None else max(current, value)
    return [
        {"valid": hour, "inches": round(value, 3)}
        for hour, value in sorted(by_hour.items())
    ]


def fetch_hourly_station_max(buffer, token: str, hours_back: int = 24) -> dict:
    """Hourly precipitation for each station in the buffer, plus the hourly maximum."""
    if not token:
        raise RuntimeError("SYNOPTIC_TOKEN is not set. Add it to .env and restart the app.")
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=hours_back)
    minx, miny, maxx, maxy = buffer.bounds
    params = {
        "bbox": f"{minx},{miny},{maxx},{maxy}",
        "pmode": "intervals",
        "interval": "1",
        "start": start.strftime("%Y%m%d%H%M"),
        "end": end.strftime("%Y%m%d%H%M"),
        "units": "english",
        "obtimezone": "UTC",
        "token": token,
    }
    try:
        with httpx.Client(timeout=60, headers={"User-Agent": USER_AGENT}) as client:
            response = client.get(PRECIP_URL, params=params)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        raise RuntimeError(redact(str(exc))) from None

    summary = payload.get("SUMMARY") or {}
    message = str(summary.get("RESPONSE_MESSAGE") or "")
    if "no stations" in message.lower():
        return {"station": [], "stations": []}
    if summary.get("RESPONSE_CODE") not in (1, None) and not payload.get("STATION"):
        raise RuntimeError(redact(message or "Synoptic precipitation request failed"))

    prepare(buffer)
    stations = []
    by_hour: dict[str, list[float]] = {}
    for raw in payload.get("STATION") or []:
        try:
            point = Point(float(raw["LONGITUDE"]), float(raw["LATITUDE"]))
        except (KeyError, TypeError, ValueError):
            continue
        if not buffer.covers(point):
            continue
        points = hourly_totals((raw.get("OBSERVATIONS") or {}).get("precipitation") or [])
        if not points:
            continue
        stations.append(
            {
                "stid": raw.get("STID"),
                "name": raw.get("NAME"),
                "points": points,
            }
        )
        for item in points:
            by_hour.setdefault(item["valid"], []).append(item["inches"])
    return {
        "station": [
            {"valid": stamp, "inches": round(max(values), 3)}
            for stamp, values in sorted(by_hour.items())
        ],
        "stations": stations,
    }
