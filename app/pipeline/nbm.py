"""National Blend of Models deterministic precipitation for CONUS.

The value for each interval is the wettest total of that length inside the
forecast horizon, then the maximum cell inside the fire buffer. Each forecast
hour is a GRIB2 file with many fields. Only the deterministic QPF message is
downloaded, using the byte range in the index. Source values are kilograms per
square meter and are converted to inches. Six-hour and longer totals use the
6-hour QPF. One-hour totals use the hourly QPF. A 3-hour total is the wettest
sum of three consecutive hours.
"""

from __future__ import annotations

import re
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np
import pyproj
import shapely
import shapely.ops

from app.net import USER_AGENT, redact
from app.paths import CACHE_DIR
from app.pipeline.mrms import shift_transform, subset_window, window_max

MM_PER_INCH = 25.4
BASE = "https://noaa-nbm-grib2-pds.s3.amazonaws.com"
COG_BASE = "https://noaa-nbm-pds.s3.amazonaws.com"
COG_ROOT = "blendv5.0/conus"
_CYCLES = ("18", "12", "06", "00")
_HOUR_PREFIX = re.compile(r"/(\d{4})/$")


def one_hour_leads(horizon: int) -> list[int]:
    """Forecast hours that carry a 1-hour NBM accumulation."""
    leads = list(range(1, min(horizon, 36) + 1))
    lead = 39
    while lead <= min(horizon, 192):
        leads.append(lead)
        lead += 3
    lead = 198
    while lead <= horizon:
        leads.append(lead)
        lead += 6
    return leads


def six_hour_leads(horizon: int) -> list[int]:
    return list(range(6, horizon + 1, 6))


def accumulation_label(lead: int, duration: int) -> str:
    return f"{lead - duration}-{lead} hour acc fcst"


def find_apcp(index_text: str, label: str) -> tuple[int, int | None] | None:
    """Byte range of the deterministic APCP message with this accumulation label."""
    rows = []
    for line in index_text.splitlines():
        parts = line.split(":")
        if len(parts) < 6 or not parts[1].isdigit():
            continue
        rows.append((int(parts[1]), parts))
    for index, (offset, parts) in enumerate(rows):
        if parts[3] != "APCP" or parts[4] != "surface" or parts[5] != label:
            continue
        if len(parts) > 6 and parts[6] != "":
            continue
        end = rows[index + 1][0] - 1 if index + 1 < len(rows) else None
        return offset, end
    return None


def _core_prefix(date: str, cycle: str, lead: int) -> str:
    return (
        f"{BASE}/blend.{date}/{cycle}/core/"
        f"blend.t{cycle}z.core.f{lead:03d}.co.grib2"
    )


def latest_cycle(client: httpx.Client) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    for day_offset in (0, 1):
        day = now - timedelta(days=day_offset)
        date = day.strftime("%Y%m%d")
        for cycle in _CYCLES:
            cycle_time = datetime(day.year, day.month, day.day, int(cycle), tzinfo=timezone.utc)
            if cycle_time > now:
                continue
            url = _core_prefix(date, cycle, 6) + ".idx"
            try:
                response = client.get(url)
            except Exception:
                continue
            if response.status_code == 200 and "APCP" in response.text:
                return date, cycle
    raise RuntimeError("No National Blend of Models CONUS cycle was found for the last two days.")


def _download_message(date: str, cycle: str, lead: int, duration: int):
    label = accumulation_label(lead, duration)
    url = _core_prefix(date, cycle, lead)
    path = CACHE_DIR / f"nbm_{date}_{cycle}_f{lead:03d}_{duration}h.grib2"
    if path.exists() and path.stat().st_size > 16:
        with path.open("rb") as handle:
            if handle.read(4) == b"GRIB":
                return lead, path, None
    try:
        with httpx.Client(timeout=90, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
            index = client.get(url + ".idx")
            if index.status_code == 404:
                return lead, None, None
            index.raise_for_status()
            located = find_apcp(index.text, label)
            if located is None:
                return lead, None, None
            start, end = located
            byte_range = f"bytes={start}-" if end is None else f"bytes={start}-{end}"
            message = client.get(url, headers={"Range": byte_range})
            message.raise_for_status()
            if not message.content.startswith(b"GRIB"):
                return lead, None, f"NBM f{lead:03d} did not return a GRIB message"
            path.write_bytes(message.content)
    except Exception as exc:
        path.unlink(missing_ok=True)
        return lead, None, redact(str(exc))
    return lead, path, None


def _mask_precip(array) -> np.ndarray:
    values = np.asarray(array, dtype=np.float32)
    invalid = ~np.isfinite(values) | (values < 0) | (values >= 9999)
    values[invalid] = -1
    return values


def _grid_frame(path):
    from osgeo import gdal

    dataset = gdal.Open(str(path))
    if dataset is None:
        raise RuntimeError(f"GDAL could not open {path.name}")
    try:
        return (
            dataset.RasterXSize,
            dataset.RasterYSize,
            dataset.GetGeoTransform(),
            dataset.GetProjection(),
        )
    finally:
        dataset = None


def _read_grid(path, window):
    from osgeo import gdal

    dataset = gdal.Open(str(path))
    if dataset is None:
        raise RuntimeError(f"GDAL could not open {path.name}")
    try:
        band = dataset.GetRasterBand(1)
        col0, row0, col1, row1 = window
        array = _mask_precip(band.ReadAsArray(col0, row0, col1 - col0, row1 - row0))
        nodata = band.GetNoDataValue()
        if nodata is not None and np.isfinite(nodata):
            array[np.isclose(array, nodata)] = -1
        valid = array >= 0
        array[valid] = array[valid] / np.float32(MM_PER_INCH)
        transform = shift_transform(dataset.GetGeoTransform(), col0, row0)
        return array, transform
    finally:
        dataset = None


def _project(polygon, projection: str):
    target = pyproj.CRS.from_wkt(projection)
    transform = pyproj.Transformer.from_crs("EPSG:4326", target, always_xy=True).transform
    return shapely.make_valid(shapely.ops.transform(transform, polygon))


def _sum_grids(arrays: list[np.ndarray]) -> np.ndarray:
    stack = np.stack(arrays)
    missing = np.any(stack < 0, axis=0)
    total = np.where(stack < 0, 0, stack).sum(axis=0)
    total[missing] = -1
    return total


def _buffer_inches(polygons, array, transform) -> list[float | None]:
    """Maximum in the buffer. ``array`` is already in inches."""
    amounts = []
    for polygon in polygons:
        inches = window_max(array, transform, polygon)
        amounts.append(None if inches is None else float(inches))
    return amounts


def newest_hour(prefixes: list[str]) -> str | None:
    hours = [match.group(1) for prefix in prefixes if (match := _HOUR_PREFIX.search(prefix))]
    return max(hours) if hours else None


def cog_url(run: datetime, product: str, valid: datetime) -> str:
    folder = f"{COG_ROOT}/{run:%Y/%m/%d}/{run:%H}00/{product}"
    name = f"blendv5.0_conus_{product}_{run:%Y-%m-%dT%H:%M}_{valid:%Y-%m-%dT%H:%M}.tif"
    return f"{COG_BASE}/{folder}/{name}"


def cog_hourly_valids(run: datetime, horizon_hours: int) -> list[datetime]:
    return [run + timedelta(hours=lead) for lead in range(1, horizon_hours + 1)]


def cog_six_hour_valids(run: datetime, horizon_hours: int) -> list[datetime]:
    """6-hour QPF ending on 00, 06, 12, or 18Z, with the whole window after the run."""
    end = run + timedelta(hours=horizon_hours)
    cursor = run.replace(minute=0, second=0, microsecond=0) + timedelta(hours=6)
    while cursor.hour % 6 != 0:
        cursor += timedelta(hours=1)
    valids = []
    while cursor <= end:
        if cursor - timedelta(hours=6) >= run:
            valids.append(cursor)
        cursor += timedelta(hours=6)
    return valids


def _child_prefixes(client: httpx.Client, prefix: str) -> list[str]:
    response = client.get(
        f"{COG_BASE}/",
        params={"list-type": "2", "delimiter": "/", "prefix": prefix, "max-keys": "1000"},
    )
    response.raise_for_status()
    return re.findall(r"<Prefix>([^<]+)</Prefix>", response.text)


def latest_cog_run(client: httpx.Client, now: datetime | None = None) -> datetime:
    """Newest CONUS COG run that has the first hourly QPF file."""
    moment = now or datetime.now(timezone.utc)
    for day_offset in (0, 1, 2):
        day = moment - timedelta(days=day_offset)
        prefix = f"{COG_ROOT}/{day:%Y/%m/%d}/"
        try:
            hours = _child_prefixes(client, prefix)
        except Exception:
            continue
        stamps = []
        for hour in hours:
            match = _HOUR_PREFIX.search(hour)
            if not match:
                continue
            text = match.group(1)
            stamp = datetime(day.year, day.month, day.day, int(text[:2]), int(text[2:]), tzinfo=timezone.utc)
            if stamp <= moment:
                stamps.append(stamp)
        for stamp in sorted(stamps, reverse=True):
            url = cog_url(stamp, "qpf01", stamp + timedelta(hours=1))
            try:
                response = client.head(url)
            except Exception:
                continue
            if response.status_code == 200:
                return stamp
    raise RuntimeError("No National Blend of Models COG run was found for the last three days.")


def _open_cog(url: str):
    from osgeo import gdal

    dataset = gdal.Open("/vsicurl/" + url)
    if dataset is None:
        raise RuntimeError(f"Could not open {url}")
    return dataset


def _cog_frame(url: str):
    dataset = _open_cog(url)
    try:
        return dataset.RasterXSize, dataset.RasterYSize, dataset.GetGeoTransform(), dataset.GetProjection()
    finally:
        dataset = None


def _read_cog(url: str, window):
    dataset = _open_cog(url)
    try:
        col0, row0, col1, row1 = window
        array = _mask_precip(dataset.GetRasterBand(1).ReadAsArray(col0, row0, col1 - col0, row1 - row0))
        return array, shift_transform(dataset.GetGeoTransform(), col0, row0)
    finally:
        dataset = None


def _update_max(best: list[float | None], amounts: list[float | None]) -> None:
    for index, inches in enumerate(amounts):
        if inches is None:
            continue
        current = best[index]
        best[index] = inches if current is None else max(current, inches)


def _record_qpf(fires, lead: int, duration: int, amounts: list[float | None], cycle_time: datetime) -> None:
    valid = (cycle_time + timedelta(hours=lead)).strftime("%Y-%m-%dT%H:%M:%SZ")
    point = {"valid": valid, "hours": duration}
    for fire, inches in zip(fires, amounts):
        fire["props"]["qpf_series"].append(
            {**point, "inches": None if inches is None else round(float(inches), 3)}
        )


def _consecutive(leads: list[int], step: int) -> bool:
    if len(leads) < 2:
        return True
    return all(right - left == step for left, right in zip(leads, leads[1:]))


def _apply(fires, hour: int, best: list[float | None], grid_count: int) -> None:
    key = str(hour)
    for fire, value in zip(fires, best):
        fire["props"]["forecast_in"][key] = None if value is None else round(value, 3)
        fire["props"]["forecast_cells"] = grid_count


def _prepare_cog():
    from osgeo import gdal

    gdal.UseExceptions()
    gdal.SetConfigOption("GDAL_PAM_ENABLED", "NO")
    gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    gdal.SetConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif")
    gdal.SetConfigOption("GDAL_HTTP_MERGE_CONSECUTIVE_RANGES", "YES")


def attach_nbm(fires: list, hours: list[int], horizon_hours: int, progress) -> str:
    """Fill forecast_in from NBM GRIB2 QPF messages. Returns the note for the page."""
    if not fires:
        return ""
    from osgeo import gdal

    gdal.UseExceptions()
    gdal.SetConfigOption("GDAL_PAM_ENABLED", "NO")
    with httpx.Client(timeout=45, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
        date, cycle = latest_cycle(client)
    cycle_time = datetime.strptime(date + cycle, "%Y%m%d%H").replace(tzinfo=timezone.utc)
    cycle_label = cycle_time.strftime("%Y-%m-%d %HZ")
    for fire in fires:
        fire["props"]["qpf_series"] = []

    six_hours = [hour for hour in hours if hour % 6 == 0]
    need_hourly = any(hour in (1, 3) for hour in hours)
    polygons = None
    transform = None
    read_window = None
    grid_count = 0
    skipped = 0
    download_errors: list[str] = []

    def load_leads(leads: list[int], duration: int, label: str):
        nonlocal polygons, transform, read_window, grid_count, skipped
        loaded = []
        progress(f"Downloading NBM {label} from {cycle_label}")
        paths: dict[int, object] = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(_download_message, date, cycle, lead, duration) for lead in leads
            ]
            finished = 0
            for future in as_completed(futures):
                finished += 1
                lead, path, error = future.result()
                if error:
                    skipped += 1
                    if error not in download_errors and len(download_errors) < 3:
                        download_errors.append(error)
                elif path is not None:
                    paths[lead] = path
                else:
                    skipped += 1
                progress(f"NBM {label} message {finished} of {len(leads)} ({cycle_label})")
        for lead in leads:
            path = paths.get(lead)
            if path is None:
                continue
            try:
                if polygons is None:
                    width, height, full_transform, projection = _grid_frame(path)
                    projected = [_project(fire["buffer"], projection) for fire in fires]
                    window = subset_window(full_transform, width, height, projected)
                    if window is None:
                        raise RuntimeError(
                            "None of the fire buffers intersect the National Blend of Models CONUS grid."
                        )
                    polygons = projected
                    read_window = window
                array, grid_transform = _read_grid(path, read_window)
            except RuntimeError as exc:
                if "intersect" in str(exc):
                    raise
                skipped += 1
                message = redact(str(exc))
                if message not in download_errors and len(download_errors) < 3:
                    download_errors.append(message)
                continue
            except Exception as exc:
                skipped += 1
                message = redact(str(exc))
                if message not in download_errors and len(download_errors) < 3:
                    download_errors.append(message)
                continue
            if transform is None:
                transform = grid_transform
            loaded.append((lead, array))
            grid_count += 1
            progress(f"NBM {label} through hour {lead} ({cycle_label})")
        return loaded

    if six_hours:
        loaded = load_leads(six_hour_leads(horizon_hours), 6, "6-hour QPF")
        best = {hour: [None] * len(fires) for hour in six_hours}
        window = max(six_hours) // 6
        recent: deque[tuple[int, np.ndarray]] = deque(maxlen=window)
        for lead, array in loaded:
            amounts = _buffer_inches(polygons, array, transform)
            _record_qpf(fires, lead, 6, amounts, cycle_time)
            recent.append((lead, array))
            for hour in six_hours:
                count = hour // 6
                if len(recent) < count:
                    continue
                chunk = list(recent)[-count:]
                if not _consecutive([item[0] for item in chunk], 6):
                    continue
                total = chunk[0][1] if count == 1 else _sum_grids([item[1] for item in chunk])
                _update_max(best[hour], _buffer_inches(polygons, total, transform))
        for hour in six_hours:
            _apply(fires, hour, best[hour], grid_count)

    if need_hourly:
        loaded = load_leads(one_hour_leads(horizon_hours), 1, "1-hour QPF")
        best_1 = [None] * len(fires)
        best_3 = [None] * len(fires)
        recent = deque(maxlen=3)
        if polygons is None:
            loaded = []
        for lead, array in loaded:
            amounts = _buffer_inches(polygons, array, transform)
            _record_qpf(fires, lead, 1, amounts, cycle_time)
            if 1 in hours:
                _update_max(best_1, amounts)
            if recent and lead - recent[-1][0] != 1:
                recent.clear()
            recent.append((lead, array))
            if 3 in hours and len(recent) == 3 and _consecutive([item[0] for item in recent], 1):
                _update_max(best_3, _buffer_inches(polygons, _sum_grids([item[1] for item in recent]), transform))
        if 1 in hours:
            _apply(fires, 1, best_1, grid_count)
        if 3 in hours:
            _apply(fires, 3, best_3, grid_count)

    for fire in fires:
        fire["props"]["qpf_series"].sort(key=lambda point: (point["valid"], point["hours"]))

    if grid_count == 0:
        raise RuntimeError(f"NBM {cycle_label} had no deterministic precipitation grids in the horizon.")

    blank = [
        str(hour)
        for hour in hours
        if all(fire["props"]["forecast_in"].get(str(hour)) is None for fire in fires)
    ]
    note = (
        f"Forecast is the National Blend of Models deterministic QPF, cycle {cycle_label}. "
        f"Only that QPF message is downloaded from each GRIB2 file. Values are kilograms per "
        f"square meter, converted to inches, and are the wettest total of that length inside "
        f"the next {horizon_hours} hours, using the maximum cell in the buffer. "
        f"Six-hour and longer totals are built from the 6-hour amounts. "
        f"One-hour and three-hour totals use the hourly amounts. "
        f"Alaska, Hawaii, and Puerto Rico are outside this CONUS grid."
    )
    if blank:
        note += " No NBM total was available for " + ", ".join(f"{hour}h" for hour in blank) + "."
    if skipped:
        note += f" {skipped} forecast hours were not in this cycle."
    if download_errors:
        note += " " + download_errors[0]
    return note
