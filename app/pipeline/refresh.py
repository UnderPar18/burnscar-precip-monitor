from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

import shapely
import shapely.geometry

from app.net import redact
from app.pipeline.geometry import buffer_geometry
from app.pipeline.mrms import apply_mrms
from app.pipeline.nbm import attach_nbm
from app.pipeline.nws import assign_point_limits, attach_forecasts
from app.pipeline.perimeters import _number, _state_code, _when, fetch_perimeters, group_perimeters
from app.pipeline.synoptic import fetch_stations_in_buffer, station_precip, synoptic_token


def _blank(hours: list[int]) -> dict[str, None]:
    return {str(hour): None for hour in hours}


def _public(fire: dict) -> dict:
    props = dict(fire["props"])
    props["perimeter"] = shapely.geometry.mapping(fire["perimeter"])
    props["buffer"] = shapely.geometry.mapping(fire["buffer"])
    return props


def _add_error(errors: list[str], message: str) -> None:
    message = redact(message).strip()
    if message and message not in errors and len(errors) < 8:
        errors.append(message)


def build_fires(features: list[dict], settings, errors: list[str]) -> list[dict]:
    fires = []
    for grouped in group_perimeters(features):
        props_in = grouped["properties"]
        name = props_in.get("attr_IncidentName") or props_in.get("poly_IncidentName") or "Unnamed"
        try:
            buffered = buffer_geometry(grouped["geometry"], settings.buffer_miles)
        except Exception as exc:
            _add_error(errors, f"{name}: buffer failed ({exc})")
            continue
        if buffered.is_empty:
            continue
        reported = _number(props_in.get("attr_IncidentSize"))
        gis_acres = _number(props_in.get("poly_GISAcres"))
        acres = reported if reported and reported > 0 else gis_acres
        fire_id = str(grouped["id"])
        fires.append(
            {
                "id": fire_id,
                "acres": acres or 0,
                "perimeter": grouped["geometry"],
                "buffer": buffered,
                "props": {
                    "id": fire_id,
                    "name": name,
                    "state": _state_code(props_in.get("attr_POOState")),
                    "county": props_in.get("attr_POOCounty"),
                    "acres": acres,
                    "gis_acres": gis_acres,
                    "contained_pct": _number(props_in.get("attr_PercentContained")),
                    "discovered": _when(props_in.get("attr_FireDiscoveryDateTime")),
                    "incident_type": props_in.get("attr_IncidentTypeCategory"),
                    "category": props_in.get("poly_FeatureCategory"),
                    "mrms_in": _blank(settings.accum_hours),
                    "station_in": _blank(settings.accum_hours),
                    "forecast_in": _blank(settings.accum_hours),
                    "station_count": 0,
                    "stations": [],
                    "forecast_cells": 0,
                    "qpf_series": [],
                },
            }
        )
    fires.sort(key=lambda fire: fire["acres"] or 0, reverse=True)
    return fires


def run_refresh(store) -> None:
    generation = store.start_refresh()
    settings = store.current_settings()
    errors: list[str] = []
    snapshot = {
        "updated_at": None,
        "refreshing": True,
        "status": "Downloading current NIFC perimeters",
        "errors": errors,
        "settings": settings.to_dict(),
        "fires": [],
        "forecast_note": "",
        "fire_count": 0,
    }
    if not store.publish(snapshot, generation):
        return

    try:
        features = fetch_perimeters(settings)
    except Exception as exc:
        snapshot["refreshing"] = False
        snapshot["status"] = "Perimeter download failed"
        _add_error(errors, str(exc))
        store.publish(snapshot, generation)
        return

    fires = build_fires(features, settings, errors)
    snapshot["fires"] = [_public(fire) for fire in fires]
    snapshot["fire_count"] = len(fires)
    snapshot["status"] = f"Buffered {len(fires)} perimeters. Requesting Synoptic stations."
    if not store.publish(snapshot, generation):
        return

    token = synoptic_token()
    if not token:
        _add_error(errors, "SYNOPTIC_TOKEN is not set. Add it to .env and restart the app.")
    elif fires:
        def load_stations(fire):
            stations = fetch_stations_in_buffer(fire["buffer"], settings.accum_hours, token)
            return fire["id"], stations

        station_errors = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(load_stations, fire) for fire in fires]
            for index, future in enumerate(as_completed(futures), start=1):
                if not store.current(generation):
                    return
                try:
                    fire_id, stations = future.result()
                except Exception as exc:
                    station_errors += 1
                    if station_errors <= 3:
                        _add_error(errors, f"Synoptic: {exc}")
                    continue
                matched = next(fire for fire in fires if fire["id"] == fire_id)
                matched["props"]["stations"] = stations
                matched["props"]["station_count"] = len(stations)
                matched["props"]["station_in"] = {
                    hour: (None if value is None else round(value, 3))
                    for hour, value in station_precip(stations, settings.accum_hours).items()
                }
                if index % 5 == 0:
                    snapshot["status"] = f"Synoptic stations {index} of {len(fires)}"
                    snapshot["fires"] = [_public(fire) for fire in fires]
                    store.publish(snapshot, generation)
        snapshot["fires"] = [_public(fire) for fire in fires]
        snapshot["status"] = "Reading MRMS precipitation grids"
        if not store.publish(snapshot, generation):
            return

    if fires and store.current(generation):
        def report(message: str) -> None:
            if not store.current(generation):
                return
            snapshot["status"] = message
            snapshot["fires"] = [_public(fire) for fire in fires]
            store.publish(snapshot, generation)

        try:
            apply_mrms(fires, settings.accum_hours, report)
        except Exception as exc:
            _add_error(errors, f"MRMS: {exc}")
        if not store.current(generation):
            return
        snapshot["fires"] = [_public(fire) for fire in fires]
        store.publish(snapshot, generation)

    if fires and store.current(generation):
        use_nbm = settings.forecast_source != "nws"
        snapshot["status"] = "Downloading NBM precipitation" if use_nbm else "Sampling NWS forecast grids"
        snapshot["fires"] = [_public(fire) for fire in fires]
        store.publish(snapshot, generation, touch=False)

        def report(message: str) -> None:
            if not store.current(generation):
                return
            snapshot["status"] = message
            snapshot["fires"] = [_public(fire) for fire in fires]
            store.publish(snapshot, generation)

        try:
            if use_nbm:
                snapshot["forecast_note"] = attach_nbm(
                    fires, settings.accum_hours, settings.forecast_hours, report
                )
            else:
                limits = assign_point_limits(fires)
                snapshot["forecast_note"] = attach_forecasts(
                    fires, settings.accum_hours, settings.forecast_hours, limits
                )
        except Exception as exc:
            _add_error(errors, f"Forecast: {exc}")
        snapshot["fires"] = [_public(fire) for fire in fires]

    snapshot["refreshing"] = False
    snapshot["status"] = f"Up to date · {len(fires)} fires"
    snapshot["fire_count"] = len(fires)
    snapshot["errors"] = errors
    store.publish(snapshot, generation)
