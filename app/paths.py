from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
STATIC_DIR = ROOT / "app" / "static"
CONFIG_PATH = ROOT / "config.yaml"
SETTINGS_PATH = DATA_DIR / "settings.json"
SNAPSHOT_PATH = DATA_DIR / "snapshot.json"
ENV_PATH = ROOT / ".env"
