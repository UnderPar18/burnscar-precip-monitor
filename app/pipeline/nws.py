from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

from app.net import USER_AGENT, redact
from app.pipeline.forecast import inches_from_qpf, parse_valid_interval, rolling_max_inches

POINTS_URL = "https://api.weather.gov/points/{lat},{lon}"
_rate_lock = threading.Lock()
_next_request = 0.0


def _pace() -> None:
    global _next_request
    with _rate_lock:
        now = time.monotonic()
        delay = max(0.0, _next_request - now)
        _next_request = max(now, _next_request) + 0.4
    if delay:
        time.sleep(delay)


def _get_json(client: httpx.Client, url: str) -> dict:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
    last_error = "NWS request failed"
    for _attempt in range(4):
        _pace()
        try:
            response = client.get(url, headers=headers)
        except Exception as exc:
            last_error = redact(str(exc))
            time.sleep(1)
            continue
        if response.status_code == 429:
            time.sleep(float(response.headers.get("Retry-After", "2")))
            last_error = "NWS rate limit"
            continue
        if response.status_code >= 400:
            last_error = f"NWS HTTP {response.status_code}"
            if response.status_code >= 500:
                time.sleep(1)
                continue
            raise RuntimeError(last_error)
        return response.json()
    raise RuntimeError(last_error)


def grid_for_point(client: httpx.Client, lat: float, lon: float) -> dict | None:
    payload = _get_json(client, POINTS_URL.format(lat=round(lat, 4), lon=round(lon, 4)))
    props = payload.get("properties") or {}
    url = props.get("forecastGridData")
    grid_id = props.get("gridId")
    if not url or grid_id is None or props.get("gridX") is None:
        return None
    return {
        "key": f"{grid_id}/{props['gridX']}/{props['gridY']}",
        "url": url,
    }


def summarize_grid(client: httpx.Client, url: str, hours: list[int], horizon_hours: int, now: datetime) -> dict:
    payload = _get_json(client, url)
    props = payload.get("properties") or {}
    qpf = props.get("quantitativePrecipitation") or {}
    unit = qpf.get("uom")
    buckets = []
    durations = set()
    for item in qpf.get("values") or []:
        spec = item.get("validTime")
        if not spec:
            continue
        try:
            start, end = parse_valid_interval(spec)
        except ValueError:
            continue
        durations.add(int(round((end - start).total_seconds() / 3600)))
        buckets.append((start, end, inches_from_qpf(item.get("value"), unit)))
    totals = {
        str(hour): rolling_max_inches(buckets, hour, horizon_hours, now)
        for hour in hours
    }
    horizon_end = now + timedelta(hours=horizon_hours)
    series = []
    for start, end, value in buckets:
        if end <= now or start >= horizon_end:
            continue
        duration = int(round((end - start).total_seconds() / 3600))
        series.append(
            {
                "valid": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "hours": duration,
                "inches": None if value is None else round(float(value), 3),
            }
        )
    return {"totals": totals, "steps": sorted(step for step in durations if step > 0), "series": series}


def assign_point_limits(fires: list, budget: int = 120, per_fire: int = 5) -> dict[str, int]:
    if not fires:
        return {}
    ranked = sorted(fires, key=lambda fire: fire["acres"] or 0, reverse=True)
    if len(fires) >= budget:
        allowed = {fire["id"] for fire in ranked[:budget]}
        return {fire["id"]: (1 if fire["id"] in allowed else 0) for fire in fires}
    limits = {fire["id"]: 1 for fire in fires}
    remaining = budget - len(fires)
    for fire in ranked:
        extra = min(per_fire - 1, remaining)
        limits[fire["id"]] += extra
        remaining -= extra
        if remaining <= 0:
            break
    return limits


def attach_forecasts(fires: list, hours: list[int], horizon_hours: int, limits: dict[str, int]) -> str:
    """Fill forecast_in on each fire. Returns a short note about the grid step."""
    from app.pipeline.geometry import sample_points

    now = datetime.now(timezone.utc)
    jobs = []
    for fire in fires:
        count = limits.get(fire["id"], 0)
        if count <= 0:
            continue
        for point in sample_points(fire["buffer"], count):
            jobs.append((fire["id"], point.y, point.x))

    fire_cells = {fire["id"]: set() for fire in fires}
    cell_urls = {}
    steps = set()
    errors = []
    with httpx.Client(timeout=45) as client:
        point_cache = {}
        for fire_id, lat, lon in jobs:
            cache_key = f"{lat:.3f},{lon:.3f}"
            try:
                if cache_key not in point_cache:
                    point_cache[cache_key] = grid_for_point(client, lat, lon)
                meta = point_cache[cache_key]
            except Exception as exc:
                errors.append(redact(str(exc)))
                continue
            if not meta:
                continue
            fire_cells[fire_id].add(meta["key"])
            cell_urls[meta["key"]] = meta["url"]

        summaries = {}
        for key, url in cell_urls.items():
            try:
                summaries[key] = summarize_grid(client, url, hours, horizon_hours, now)
                steps.update(summaries[key]["steps"])
            except Exception as exc:
                errors.append(redact(str(exc)))

    for fire in fires:
        cells = [summaries[key] for key in fire_cells[fire["id"]] if key in summaries]
        fire["props"]["forecast_cells"] = len(cells)
        merged: dict[tuple[str, int], float | None] = {}
        for cell in cells:
            for point in cell.get("series") or []:
                key = (point["valid"], int(point["hours"]))
                amount = point.get("inches")
                if key not in merged or amount is None:
                    merged.setdefault(key, None if amount is None else float(amount))
                elif merged[key] is None:
                    merged[key] = float(amount)
                else:
                    merged[key] = max(merged[key], float(amount))
        fire["props"]["qpf_series"] = [
            {"valid": valid, "hours": length, "inches": None if amount is None else round(amount, 3)}
            for (valid, length), amount in sorted(merged)
        ]
        for hour in hours:
            values = [
                cell["totals"].get(str(hour))
                for cell in cells
                if cell["totals"].get(str(hour)) is not None
            ]
            fire["props"]["forecast_in"][str(hour)] = round(max(values), 3) if values else None

    if errors:
        fires_note = errors[0]
    else:
        fires_note = ""
    if steps == {6} or 6 in steps:
        step_note = (
            "NWS quantitative precipitation arrives in 6-hour amounts, in inches after "
            "converting from millimeters. Intervals shorter than that step are left blank. "
            "Longer intervals are the wettest run of those 6-hour amounts inside the "
            f"next {horizon_hours} hours, using up to 5 sampled grid cells in each buffer."
        )
    elif steps:
        listed = ", ".join(str(step) for step in sorted(steps))
        step_note = (
            f"NWS quantitative precipitation steps in this run: {listed} hours. "
            f"Values are the wettest window inside the next {horizon_hours} hours."
        )
    else:
        step_note = "NWS forecast grids were not available for the sampled points."
    if fires_note and fires_note not in step_note:
        step_note = f"{step_note} {fires_note}"
    return step_note
