from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

import shapely.geometry
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings_from_dict
from app.net import redact
from app.paths import STATIC_DIR
from app.pipeline.mrms import hourly_qpe
from app.pipeline.refresh import run_refresh
from app.pipeline.synoptic import fetch_hourly_station_max, synoptic_token
from app.store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("burnscar")

store = Store()


def _loop() -> None:
    while True:
        try:
            run_refresh(store)
        except Exception as exc:
            from app.net import redact

            log.error("Refresh failed: %s", redact(str(exc)))
            store.record_failure(str(exc))
        store.wait(store.current_settings().refresh_minutes)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    thread = threading.Thread(target=_loop, name="burnscar-refresh", daemon=True)
    thread.start()
    yield


app = FastAPI(title="Burn scar precip monitor", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/snapshot")
def snapshot():
    return store.read()


@app.post("/api/refresh")
def refresh_now():
    store.request_refresh()
    return {"ok": True}


@app.get("/api/series")
def observed_series(fire_id: str):
    """Past 24 hours of MRMS 1-hour QPE and the max station total, for one buffer."""
    snapshot = store.read()
    fire = next((item for item in snapshot.get("fires") or [] if item.get("id") == fire_id), None)
    if fire is None or not fire.get("buffer"):
        raise HTTPException(status_code=404, detail="That fire is not in the current list.")
    polygon = shapely.geometry.shape(fire["buffer"])
    payload = {"mrms": [], "station": [], "stations": [], "errors": []}
    try:
        payload["mrms"] = hourly_qpe(polygon, 24)
    except Exception as exc:
        payload["errors"].append("MRMS: " + redact(str(exc)))
    token = synoptic_token()
    if not token:
        payload["errors"].append("SYNOPTIC_TOKEN is not set.")
    else:
        try:
            observed = fetch_hourly_station_max(polygon, token, 24)
            payload["station"] = observed["station"]
            payload["stations"] = observed["stations"]
        except Exception as exc:
            payload["errors"].append("Synoptic: " + redact(str(exc)))
    return payload


@app.post("/api/settings")
def update_settings(payload: dict):
    try:
        settings = settings_from_dict(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    store.replace_settings(settings)
    return {"ok": True, "settings": settings.to_dict()}
