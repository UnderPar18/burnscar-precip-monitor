from __future__ import annotations

import copy
import json
import threading

from app.config import Settings, load_settings, save_settings
from app.net import redact
from app.paths import SNAPSHOT_PATH


class Store:
    def __init__(self):
        self._lock = threading.Lock()
        self._generation = 0
        self._revision = 0
        self._event = threading.Event()
        self.settings = load_settings()
        self.snapshot = self._load_snapshot()

    def _load_snapshot(self) -> dict:
        base = self._empty_snapshot(self.settings)
        if not SNAPSHOT_PATH.exists():
            return base
        try:
            saved = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return base
        saved["refreshing"] = False
        saved["settings"] = self.settings.to_dict()
        if saved.get("fires"):
            saved["status"] = "Showing the last saved run while a new one starts"
        return saved

    def _empty_snapshot(self, settings: Settings) -> dict:
        return {
            "updated_at": None,
            "refreshing": False,
            "status": "Starting",
            "errors": [],
            "settings": settings.to_dict(),
            "fires": [],
            "forecast_note": "",
            "fire_count": 0,
        }

    def current_settings(self) -> Settings:
        with self._lock:
            return self.settings

    def current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation

    def start_refresh(self) -> int:
        with self._lock:
            self._generation += 1
            return self._generation

    def request_refresh(self) -> None:
        with self._lock:
            self._generation += 1
        self._event.set()

    def replace_settings(self, settings: Settings) -> None:
        save_settings(settings)
        with self._lock:
            self.settings = settings
            self._generation += 1
        self._event.set()

    def wait(self, minutes: int) -> None:
        self._event.wait(timeout=max(minutes, 1) * 60)
        self._event.clear()

    def publish(self, snapshot: dict, generation: int, touch: bool = True) -> bool:
        if not self.current(generation):
            return False
        payload = copy.deepcopy(snapshot)
        with self._lock:
            if generation != self._generation:
                return False
            self._revision += 1
            payload["revision"] = self._revision
            if touch or not payload.get("updated_at"):
                from datetime import datetime, timezone

                payload["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            self.snapshot = payload
        self._write(payload)
        return True

    def record_failure(self, message: str) -> None:
        with self._lock:
            snapshot = copy.deepcopy(self.snapshot)
            snapshot["refreshing"] = False
            snapshot["status"] = "Refresh failed"
            errors = list(snapshot.get("errors") or [])
            text = redact(message)
            if text not in errors:
                errors.append(text)
            snapshot["errors"] = errors[:8]
            self.snapshot = snapshot
        self._write(snapshot)

    def _write(self, snapshot: dict) -> None:
        try:
            SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary = SNAPSHOT_PATH.with_suffix(".tmp")
            temporary.write_text(json.dumps(snapshot), encoding="utf-8")
            temporary.replace(SNAPSHOT_PATH)
        except OSError:
            return

    def read(self) -> dict:
        with self._lock:
            return self.snapshot
