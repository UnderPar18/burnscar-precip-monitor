from __future__ import annotations

import gzip
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import shutil

import httpx
import numpy as np
from shapely import box, covers, points
from shapely.geometry.base import BaseGeometry

from app.net import USER_AGENT
from app.paths import CACHE_DIR

# Radar-only quantitative precipitation from NCEP, in millimeters.
# The MRMS image service export does not return this quantitative grid.
PRODUCTS = {
    1: "RadarOnly_QPE_01H",
    3: "RadarOnly_QPE_03H",
    6: "RadarOnly_QPE_06H",
    12: "RadarOnly_QPE_12H",
    24: "RadarOnly_QPE_24H",
    48: "RadarOnly_QPE_48H",
    72: "RadarOnly_QPE_72H",
}
CATALOG = "https://mrms.ncep.noaa.gov/2D/{product}/"
MM_PER_INCH = 25.4


def max_in_polygon(array, transform, polygon: BaseGeometry) -> float | None:
    """Maximum nonnegative cell value whose center falls inside the polygon.

    Negative values are nodata. ``transform`` is a GDAL geotransform.
    """
    values = np.asarray(array, dtype=float)
    if values.ndim != 2 or values.size == 0 or polygon.is_empty:
        return None
    rows, cols = values.shape
    row_idx, col_idx = np.indices((rows, cols))
    xs = transform[0] + (col_idx + 0.5) * transform[1] + (row_idx + 0.5) * transform[2]
    ys = transform[3] + (col_idx + 0.5) * transform[4] + (row_idx + 0.5) * transform[5]
    valid = np.isfinite(values) & (values >= 0)
    if not np.any(valid):
        return None
    minx, miny, maxx, maxy = polygon.bounds
    in_bbox = valid & (xs >= minx) & (xs <= maxx) & (ys >= miny) & (ys <= maxy)
    if not np.any(in_bbox):
        return None
    inside = covers(polygon, points(xs[in_bbox], ys[in_bbox]))
    chosen = values[in_bbox][np.asarray(inside)]
    if chosen.size == 0:
        return None
    return float(chosen.max())


def _latest_url(client: httpx.Client, product: str) -> str:
    response = client.get(CATALOG.format(product=product), follow_redirects=True)
    response.raise_for_status()
    names = re.findall(r"(MRMS_[A-Za-z0-9_.-]+\.grib2\.gz)", response.text)
    if not names:
        raise RuntimeError(f"No MRMS files were listed for {product}")
    return CATALOG.format(product=product) + names[-1]


def _download(client: httpx.Client, url: str, gz_path, grib_path) -> None:
    with client.stream("GET", url, follow_redirects=True) as response:
        response.raise_for_status()
        with gz_path.open("wb") as handle:
            for chunk in response.iter_bytes():
                handle.write(chunk)
    with gzip.open(gz_path, "rb") as source, grib_path.open("wb") as target:
        shutil.copyfileobj(source, target)


def window_bounds(transform, polygon: BaseGeometry, width: int, height: int):
    minx, miny, maxx, maxy = polygon.bounds
    cols = [(minx - transform[0]) / transform[1], (maxx - transform[0]) / transform[1]]
    rows = [(miny - transform[3]) / transform[5], (maxy - transform[3]) / transform[5]]
    col0 = max(0, int(np.floor(min(cols))) - 1)
    col1 = min(width, int(np.ceil(max(cols))) + 2)
    row0 = max(0, int(np.floor(min(rows))) - 1)
    row1 = min(height, int(np.ceil(max(rows))) + 2)
    return col0, row0, col1, row1


def shift_transform(transform, col0: int, row0: int):
    return (
        transform[0] + col0 * transform[1] + row0 * transform[2],
        transform[1],
        transform[2],
        transform[3] + col0 * transform[4] + row0 * transform[5],
        transform[4],
        transform[5],
    )


def subset_window(transform, width: int, height: int, polygons: list[BaseGeometry]):
    """Pixel window covering every polygon, or None when none intersect the grid."""
    usable = [polygon for polygon in polygons if polygon is not None and not polygon.is_empty]
    if not usable:
        return None
    minx = min(polygon.bounds[0] for polygon in usable)
    miny = min(polygon.bounds[1] for polygon in usable)
    maxx = max(polygon.bounds[2] for polygon in usable)
    maxy = max(polygon.bounds[3] for polygon in usable)
    col0, row0, col1, row1 = window_bounds(transform, box(minx, miny, maxx, maxy), width, height)
    if col1 <= col0 or row1 <= row0:
        return None
    return col0, row0, col1, row1


def qpe_band_number(dataset, product: str) -> int:
    """Band that holds this MRMS QPE product.

    Each RadarOnly QPE file on the MRMS server is one GRIB2 field. If a file
    ever contains more than one field, only the named product is read.
    """
    count = int(dataset.RasterCount)
    chosen = None
    for index in range(1, count + 1):
        element = dataset.GetRasterBand(index).GetMetadataItem("GRIB_ELEMENT") or ""
        if element == product:
            if chosen is not None:
                raise RuntimeError(f"MRMS {product} file contains that field more than once.")
            chosen = index
    if chosen is not None:
        return chosen
    if count == 1:
        return 1
    raise RuntimeError(f"MRMS {product} file has {count} fields and none is {product}.")


def read_band_window(dataset, window, band: int = 1):
    """Read one band for a pixel window and return the array plus its geotransform."""
    col0, row0, col1, row1 = window
    transform = dataset.GetGeoTransform()
    array = dataset.GetRasterBand(band).ReadAsArray(col0, row0, col1 - col0, row1 - row0)
    return array, shift_transform(transform, col0, row0)


def window_max(array, transform, polygon: BaseGeometry) -> float | None:
    """Maximum nonnegative cell inside the polygon, reading only the polygon's window."""
    values = np.asarray(array)
    if values.ndim != 2:
        return None
    rows, cols = values.shape
    col0, row0, col1, row1 = window_bounds(transform, polygon, cols, rows)
    if col1 <= col0 or row1 <= row0:
        return None
    window_transform = (
        transform[0] + col0 * transform[1] + row0 * transform[2],
        transform[1],
        transform[2],
        transform[3] + col0 * transform[4] + row0 * transform[5],
        transform[4],
        transform[5],
    )
    return max_in_polygon(values[row0:row1, col0:col1], window_transform, polygon)


def _window_max_mm(dataset, polygon: BaseGeometry, product: str) -> float | None:
    window = subset_window(
        dataset.GetGeoTransform(), dataset.RasterXSize, dataset.RasterYSize, [polygon]
    )
    if window is None:
        return None
    array, window_transform = read_band_window(dataset, window, qpe_band_number(dataset, product))
    return max_in_polygon(array, window_transform, polygon)


def apply_mrms(fires: list[dict], hours: list[int], progress) -> None:
    """Download one radar-only QPE grid per interval and store the buffer maximum in inches."""
    if not fires or not hours:
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    from osgeo import gdal

    gdal.UseExceptions()
    gdal.SetConfigOption("GDAL_PAM_ENABLED", "NO")
    failures = []
    with httpx.Client(timeout=180, headers={"User-Agent": USER_AGENT}) as client:
        for index, hour in enumerate(hours, start=1):
            product = PRODUCTS[hour]
            progress(f"Downloading MRMS {hour}h grid ({index} of {len(hours)})")
            gz_path = CACHE_DIR / f"{product}.grib2.gz"
            grib_path = CACHE_DIR / f"{product}.grib2"
            dataset = None
            try:
                url = _latest_url(client, product)
                _download(client, url, gz_path, grib_path)
                dataset = gdal.Open(str(grib_path))
                if dataset is None:
                    raise RuntimeError(f"GDAL could not open the {hour}h MRMS grid")
                window = subset_window(
                    dataset.GetGeoTransform(),
                    dataset.RasterXSize,
                    dataset.RasterYSize,
                    [fire["buffer"] for fire in fires],
                )
                if window is None:
                    for fire in fires:
                        fire["props"]["mrms_in"][str(hour)] = None
                else:
                    array, window_transform = read_band_window(
                        dataset, window, qpe_band_number(dataset, product)
                    )
                    for fire in fires:
                        millimeters = window_max(array, window_transform, fire["buffer"])
                        fire["props"]["mrms_in"][str(hour)] = (
                            None if millimeters is None else round(millimeters / MM_PER_INCH, 3)
                        )
            except Exception as exc:
                failures.append(f"{hour}h: {exc}")
            finally:
                dataset = None
                gz_path.unlink(missing_ok=True)
                grib_path.unlink(missing_ok=True)
            if not failures or not failures[-1].startswith(f"{hour}h:"):
                progress(f"MRMS {hour}h grid applied to {len(fires)} buffers")
    if failures:
        raise RuntimeError("MRMS " + "; ".join(failures))


_HOURLY_NAME = re.compile(r"MRMS_RadarOnly_QPE_01H_00\.00_(\d{8}-\d{6})\.grib2\.gz")


def select_hourly_files(names: list[str], now: datetime, hours_back: int = 24) -> list[tuple[datetime, str]]:
    """One 1-hour QPE file per hour, closest to the top of that hour.

    The MRMS 1-hour product is rewritten every couple of minutes. Keeping the
    newest files would show the last hour many times instead of the past day.
    """
    moment = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = moment - timedelta(hours=max(hours_back, 1) - 1)
    chosen: dict[datetime, tuple[datetime, str, int]] = {}
    for name in names:
        match = _HOURLY_NAME.search(name)
        if not match:
            continue
        when = datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
        hour = when.replace(minute=0, second=0, microsecond=0)
        if hour < start or hour > moment:
            continue
        distance = when.minute * 60 + when.second
        current = chosen.get(hour)
        if current is None or distance < current[2] or (distance == current[2] and when > current[0]):
            chosen[hour] = (when, match.group(0), distance)
    return [(when, filename) for when, filename, _distance in (chosen[hour] for hour in sorted(chosen))]


def _hourly_catalog(client: httpx.Client, hours_back: int) -> list[tuple[datetime, str]]:
    response = client.get(CATALOG.format(product=PRODUCTS[1]), follow_redirects=True)
    response.raise_for_status()
    names = [match.group(0) for match in _HOURLY_NAME.finditer(response.text)]
    return select_hourly_files(names, datetime.now(timezone.utc), hours_back)


def _cached_hourly_grib(name: str):
    folder = CACHE_DIR / "mrms_hourly"
    folder.mkdir(parents=True, exist_ok=True)
    grib_path = folder / name.replace(".gz", "")
    if grib_path.exists() and grib_path.stat().st_size > 0:
        return grib_path
    gz_path = grib_path.with_suffix(".grib2.gz")
    url = CATALOG.format(product=PRODUCTS[1]) + name
    with httpx.Client(timeout=180, headers={"User-Agent": USER_AGENT}) as client:
        _download(client, url, gz_path, grib_path)
    gz_path.unlink(missing_ok=True)
    return grib_path


def hourly_qpe(polygon: BaseGeometry, hours_back: int = 24) -> list[dict]:
    """Buffer-maximum MRMS 1-hour QPE for recent hours, in inches."""
    if polygon.is_empty:
        return []
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    from osgeo import gdal

    gdal.UseExceptions()
    gdal.SetConfigOption("GDAL_PAM_ENABLED", "NO")
    with httpx.Client(timeout=180, headers={"User-Agent": USER_AGENT}) as client:
        catalog = _hourly_catalog(client, hours_back)
        paths = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(_cached_hourly_grib, name): (when, name)
                for when, name in catalog
            }
        for future in as_completed(futures):
            when, _name = futures[future]
            try:
                paths[when] = future.result()
            except Exception:
                paths[when] = None
    series = []
    for when, _name in catalog:
        path = paths.get(when)
        dataset = gdal.Open(str(path)) if path else None
        if dataset is None:
            series.append({"valid": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "inches": None})
            continue
        try:
            millimeters = _window_max_mm(dataset, polygon, PRODUCTS[1])
        finally:
            dataset = None
        series.append(
            {
                "valid": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "inches": None if millimeters is None else round(millimeters / MM_PER_INCH, 3),
            }
        )
    return series
