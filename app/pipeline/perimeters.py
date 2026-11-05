from __future__ import annotations

from datetime import datetime, timezone

import httpx
import shapely
import shapely.geometry

from app.net import USER_AGENT, redact

PERIMETER_URL = (
    "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
    "WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query"
)
OUT_FIELDS = ",".join(
    [
        "poly_IncidentName",
        "poly_GISAcres",
        "poly_FeatureCategory",
        "attr_POOState",
        "attr_POOCounty",
        "attr_IncidentSize",
        "attr_IncidentTypeCategory",
        "attr_PercentContained",
        "attr_FireDiscoveryDateTime",
        "attr_UniqueFireIdentifier",
        "attr_IrwinID",
        "attr_IncidentName",
    ]
)


def perimeter_where(settings) -> str:
    parts = ["1=1"]
    if settings.min_acres > 0:
        acres = float(settings.min_acres)
        parts.append(f"(poly_GISAcres >= {acres} OR attr_IncidentSize >= {acres})")
    if settings.wildfire_only:
        parts.append("attr_IncidentTypeCategory = 'WF'")
    if settings.states:
        quoted = ",".join(f"'US-{code}'" for code in settings.states)
        parts.append(f"attr_POOState IN ({quoted})")
    return " AND ".join(parts)


def fetch_perimeters(settings) -> list[dict]:
    features = []
    offset = 0
    with httpx.Client(timeout=90, headers={"User-Agent": USER_AGENT}) as client:
        while True:
            params = {
                "where": perimeter_where(settings),
                "outFields": OUT_FIELDS,
                "returnGeometry": "true",
                "outSR": "4326",
                "f": "geojson",
                "orderByFields": "poly_GISAcres DESC",
                "resultOffset": offset,
                "resultRecordCount": 1000,
            }
            try:
                response = client.get(PERIMETER_URL, params=params)
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                raise RuntimeError(redact(str(exc))) from None
            if payload.get("error"):
                message = payload["error"].get("message", "WFIGS query failed")
                raise RuntimeError(message)
            page = payload.get("features") or []
            features.extend(page)
            props = payload.get("properties") or {}
            exceeded = bool(props.get("exceededTransferLimit") or payload.get("exceededTransferLimit"))
            if not exceeded or not page:
                break
            offset += len(page)
            if offset > 20000:
                break
    return features


def _number(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _when(value) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, str) and not value.replace(".", "", 1).isdigit():
        return value
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return str(value)
    if stamp > 1e12:
        stamp /= 1000.0
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _state_code(value: str | None) -> str:
    text = (value or "").upper()
    if text.startswith("US-"):
        return text[3:]
    return text


def group_perimeters(features: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    order: list[str] = []
    for feature in features:
        props = feature.get("properties") or {}
        geometry = feature.get("geometry")
        if not geometry:
            continue
        try:
            shape = shapely.make_valid(shapely.geometry.shape(geometry))
        except Exception:
            continue
        if shape.is_empty:
            continue
        key = (
            props.get("attr_IrwinID")
            or props.get("attr_UniqueFireIdentifier")
            or f"{props.get('poly_IncidentName')}|{props.get('attr_POOState')}"
        )
        key = str(key)
        if key not in grouped:
            grouped[key] = {"properties": dict(props), "geometries": [shape]}
            order.append(key)
            continue
        grouped[key]["geometries"].append(shape)
        current = grouped[key]["properties"]
        for field in ("poly_GISAcres", "attr_IncidentSize"):
            incoming = _number(props.get(field))
            held = _number(current.get(field))
            if incoming is not None and (held is None or incoming > held):
                current[field] = props.get(field)
    fires = []
    for key in order:
        item = grouped[key]
        geometry = shapely.make_valid(shapely.unary_union(item["geometries"]))
        fires.append({"id": key, "properties": item["properties"], "geometry": geometry})
    return fires
