from __future__ import annotations

import pyproj
import shapely
import shapely.ops
from shapely.geometry.base import BaseGeometry

METERS_PER_MILE = 1609.344


def _transformers(lon: float, lat: float):
    local = pyproj.CRS.from_proj4(
        f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m +no_defs"
    )
    forward = pyproj.Transformer.from_crs("EPSG:4326", local, always_xy=True).transform
    inverse = pyproj.Transformer.from_crs(local, "EPSG:4326", always_xy=True).transform
    return forward, inverse


def buffer_geometry(geom: BaseGeometry, miles: float) -> BaseGeometry:
    """Buffer in a local azimuthal equidistant projection so the distance is in miles."""
    geom = shapely.make_valid(geom)
    anchor = geom.representative_point()
    forward, inverse = _transformers(anchor.x, anchor.y)
    projected = shapely.ops.transform(forward, geom)
    buffered = projected.buffer(miles * METERS_PER_MILE)
    return shapely.make_valid(shapely.ops.transform(inverse, buffered))


def sample_points(polygon: BaseGeometry, limit: int = 5) -> list:
    """Points inside the buffer used to sample the NWS forecast grid."""
    polygon = shapely.make_valid(polygon)
    if polygon.is_empty or limit <= 0:
        return []
    anchor = polygon.representative_point()
    chosen = [anchor]
    if limit == 1:
        return chosen
    forward, inverse = _transformers(anchor.x, anchor.y)
    projected = shapely.ops.transform(forward, polygon)
    minx, miny, maxx, maxy = projected.bounds
    inset_x = (maxx - minx) * 0.2
    inset_y = (maxy - miny) * 0.2
    candidates = [
        shapely.Point(x, y)
        for x in (minx + inset_x, (minx + maxx) / 2, maxx - inset_x)
        for y in (miny + inset_y, (miny + maxy) / 2, maxy - inset_y)
    ]
    for candidate in candidates:
        point = shapely.ops.transform(inverse, candidate)
        if polygon.covers(point) and point.distance(anchor) > 1e-6:
            chosen.append(point)
        if len(chosen) >= limit:
            break
    return chosen[:limit]
