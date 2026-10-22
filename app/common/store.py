"""Gemeinsame Persistenz (JSON-Dateien im geteilten Daten-Volume).

Schreibrechte sind aufgeteilt, damit sich die zwei Container nicht in die Quere kommen:
  * Dashboard   schreibt  state.json, history.json
  * Maintenance schreibt  config.json (Einstellungen, Fahrzeuge, Vorlagen, Zeitplan)
  * secrets.json wird beim ersten Start einmalig angelegt (gemeinsames Geheimnis).
"""
from __future__ import annotations

import copy
import json
import logging
import os
import secrets
import threading
import time
import uuid
from typing import Any, Callable

logger = logging.getLogger("feuerwehr")

DATA_DIR = os.environ.get("DATA_DIR", "data")
WEB_DIR = os.environ.get("WEB_DIR", "web")
SOUNDS_DIR = os.path.join(WEB_DIR, "sounds")
os.makedirs(DATA_DIR, exist_ok=True)

STATE_PATH = os.path.join(DATA_DIR, "state.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
SECRETS_PATH = os.path.join(DATA_DIR, "secrets.json")

SOUND_EXTS = {".mp3", ".wav", ".ogg", ".m4a"}
HISTORY_MAX = 500

# Fahrzeug-Status: key = was im Alarm gespeichert wird, css = Darstellung im Dashboard
VEHICLE_STATUSES = [
    {"key": "alarmiert", "css": "out"},
    {"key": "rückt aus", "css": "out"},
    {"key": "auf Anfahrt", "css": "enroute"},
    {"key": "vor Ort", "css": "ready"},
    {"key": "einsatzbereit", "css": "ready"},
]

_lock = threading.RLock()


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def load_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return copy.deepcopy(default)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("Konnte %s nicht laden (%s). Verwende Default.", path, exc)
        return copy.deepcopy(default)


def save_json(path: str, data: Any) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


# -----------------------------------------------------------------------------
# Secrets (Session-Schlüssel + interner API-Token)
# -----------------------------------------------------------------------------
def get_secrets() -> dict:
    """Liest/legt secrets.json an. Race-sicher, falls beide Container gleichzeitig starten."""
    for _ in range(20):
        data = load_json(SECRETS_PATH, {})
        if data.get("internal_token") and data.get("session_secret"):
            return data
        fresh = {"internal_token": secrets.token_urlsafe(32), "session_secret": secrets.token_urlsafe(48)}
        try:
            fd = os.open(SECRETS_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(fresh, f)
            return fresh
        except FileExistsError:
            time.sleep(0.1)
    raise RuntimeError("secrets.json konnte nicht gelesen werden")


# -----------------------------------------------------------------------------
# Konfiguration (Einstellungen, Fahrzeuge, Vorlagen, Zeitplan)
# -----------------------------------------------------------------------------
DEFAULT_STATION = {"lat": 49.6935, "lon": 8.64841}

DEFAULT_CONFIG: dict = {
    "settings": {
        "theme_mode": "auto",            # auto (Sonnenauf-/untergang) | light | dark
        "agency_name": os.environ.get("DEFAULT_AGENCY_NAME", "Feuerwehr Bensheim-Schönberg"),
        "station": DEFAULT_STATION,
        "valid_minutes_default": int(os.environ.get("VALID_MINUTES_DEFAULT", "60")),
        "sound": None,                    # Dateiname in web/sounds, None = automatisch
        "weather_enabled": True,
    },
    "auth": {"password_hash": None},      # None = Passwort aus Umgebungsvariable
    "vehicles": [
        {"id": "tsfw", "name": "TSFW 7-48-1", "kind": "TSF-W", "active": True},
        {"id": "klkw", "name": "K-LKW 7-63-1", "kind": "K-LKW", "active": True},
        {"id": "mtw", "name": "MTW 7-18-1", "kind": "MTW", "active": True},
    ],
    "templates": [
        {
            "id": "schloss", "name": "F2 – Rauchentwicklung Schloss Schönberg",
            "stichwort": "F2 – Rauchentwicklung am Schloss Schönberg",
            "adresse": "Schloss Schönberg, Neuer Schlossweg 15, 64625 Bensheim-Schönberg",
            "lat": 49.6928, "lon": 8.646534,
            "infos": ["Treffpunkt: Toranlage am Neuer Schlossweg",
                      "Zufahrt über Nibelungenstraße → Neuer Schlossweg"],
            "vehicle_ids": ["tsfw", "klkw", "mtw"], "valid_minutes": 60, "exercise": True,
        },
        {
            "id": "wohnung", "name": "F3 – Wohnungsbrand",
            "stichwort": "F3 – Wohnungsbrand, Menschenleben in Gefahr",
            "adresse": "Hauptstraße 1, 64625 Bensheim",
            "lat": 49.6812, "lon": 8.6185,
            "infos": ["Person vermisst", "Rauch aus Fenster im 2. OG"],
            "vehicle_ids": ["tsfw", "klkw", "mtw"], "valid_minutes": 90, "exercise": True,
        },
        {
            "id": "vu", "name": "H1 – Verkehrsunfall",
            "stichwort": "H1 – Verkehrsunfall, eingeklemmte Person",
            "adresse": "B3, 64625 Bensheim",
            "lat": 49.7001, "lon": 8.6320,
            "infos": ["PKW gegen Baum", "Auslaufende Betriebsstoffe"],
            "vehicle_ids": ["tsfw", "mtw"], "valid_minutes": 60, "exercise": True,
        },
    ],
    "schedules": [],
}


def _merge_defaults(cfg: dict) -> dict:
    out = copy.deepcopy(DEFAULT_CONFIG)
    for key, val in cfg.items():
        if key == "settings" and isinstance(val, dict):
            out["settings"].update(val)
        else:
            out[key] = val
    return out


_cfg_cache: dict = {"mtime": None, "data": None}


def load_config() -> dict:
    """Liest config.json (mit mtime-Cache, damit das Dashboard sie oft abfragen darf)."""
    with _lock:
        try:
            mtime = os.path.getmtime(CONFIG_PATH)
        except OSError:
            mtime = None
        if _cfg_cache["data"] is not None and _cfg_cache["mtime"] == mtime:
            return copy.deepcopy(_cfg_cache["data"])
        data = _merge_defaults(load_json(CONFIG_PATH, {}))
        _cfg_cache.update(mtime=mtime, data=data)
        return copy.deepcopy(data)


def config_mtime() -> float | None:
    try:
        return os.path.getmtime(CONFIG_PATH)
    except OSError:
        return None


def update_config(fn: Callable[[dict], Any]) -> Any:
    """Read-modify-write unter Lock. fn bekommt das Config-Dict und darf es verändern."""
    with _lock:
        cfg = load_config()
        result = fn(cfg)
        save_json(CONFIG_PATH, cfg)
        _cfg_cache.update(mtime=config_mtime(), data=copy.deepcopy(cfg))
        return result


def public_settings(cfg: dict | None = None) -> dict:
    """Teil der Einstellungen, den das Dashboard sehen darf/soll."""
    cfg = cfg or load_config()
    s = cfg["settings"]
    return {
        "theme_mode": s["theme_mode"],
        "agency_name": s["agency_name"],
        "station": s["station"],
        "sound": s["sound"],
        "weather_enabled": s["weather_enabled"],
    }


# -----------------------------------------------------------------------------
# State / History (vom Dashboard geschrieben)
# -----------------------------------------------------------------------------
def load_state() -> dict:
    return load_json(STATE_PATH, {"current_alarm": None})


def save_state(state: dict) -> None:
    with _lock:
        save_json(STATE_PATH, state)


def load_history() -> list:
    return load_json(HISTORY_PATH, [])


def append_history(entry: dict) -> None:
    with _lock:
        data = load_history()
        data.append(entry)
        save_json(HISTORY_PATH, data[-HISTORY_MAX:])


def clear_history() -> None:
    with _lock:
        save_json(HISTORY_PATH, [])


# -----------------------------------------------------------------------------
# Sounds
# -----------------------------------------------------------------------------
def list_sounds() -> list[dict]:
    items: list[dict] = []
    if not os.path.isdir(SOUNDS_DIR):
        return items
    for name in sorted(os.listdir(SOUNDS_DIR)):
        p = os.path.join(SOUNDS_DIR, name)
        if os.path.isfile(p) and os.path.splitext(name)[1].lower() in SOUND_EXTS:
            items.append({"filename": name, "url": f"/static/sounds/{name}", "size": os.path.getsize(p)})
    return items


def effective_sound(selected: str | None) -> str | None:
    items = list_sounds()
    names = {i["filename"] for i in items}
    if selected in names:
        return selected
    for key in ("sirene", "feuerwehr"):
        for s in items:
            if key in s["filename"].lower():
                return s["filename"]
    return items[0]["filename"] if items else None
