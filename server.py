from __future__ import annotations

import os
import json
import uuid
import asyncio
import datetime
import logging
from typing import List, Optional

import requests
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

# -----------------------------------------------------------------------------
# Konfiguration / Defaults
# -----------------------------------------------------------------------------
DATA_DIR = os.environ.get("DATA_DIR", "data")
STATIC_DIR = os.environ.get("STATIC_DIR", "static")
os.makedirs(DATA_DIR, exist_ok=True)
STATE_PATH = os.path.join(DATA_DIR, "state.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")

OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org")
OSRM_TIMEOUT = float(os.environ.get("OSRM_TIMEOUT", "8.0"))  # Sekunden
VALID_MINUTES_DEFAULT = int(os.environ.get("VALID_MINUTES_DEFAULT", "60"))

# Wache Bensheim-Schönberg (Standard, wenn kein Start übergeben wird)
DEFAULT_START_LAT = float(os.environ.get("DEFAULT_START_LAT", "49.6935"))
DEFAULT_START_LON = float(os.environ.get("DEFAULT_START_LON", "8.64841"))
DEFAULT_AGENCY_NAME = os.environ.get("DEFAULT_AGENCY_NAME", "Feuerwehr Bensheim-Schönberg")

SOUND_EXTS = {".mp3", ".wav", ".ogg", ".m4a"}

# Logging
logger = logging.getLogger("feuerwehr")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# -----------------------------------------------------------------------------
# FastAPI App
# -----------------------------------------------------------------------------
app = FastAPI(title="Feuerwehr Dashboard API", version="1.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# -----------------------------------------------------------------------------
# Modelle
# -----------------------------------------------------------------------------
class Coords(BaseModel):
    lat: float
    lon: float

class FleetItem(BaseModel):
    order: int
    name: str
    status: str

class AlarmIn(BaseModel):
    agencyName: Optional[str] = None
    stichwort: str
    zeit: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))
    adresse: Optional[str] = None
    infos: List[str] = Field(default_factory=list)
    start: Optional[Coords] = None
    ziel: Optional[Coords] = None
    valid_minutes: Optional[int] = None
    fleet: List[FleetItem] = Field(default_factory=list)
    route_speed_kmh: Optional[float] = None

    @field_validator("zeit", mode="before")
    @classmethod
    def parse_dt(cls, v):
        if v is None or v == "":
            return datetime.datetime.now(datetime.timezone.utc)
        if isinstance(v, (int, float)):
            return datetime.datetime.fromtimestamp(v, tz=datetime.timezone.utc)
        if isinstance(v, str):
            try:
                if v.endswith("Z"):
                    return datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
                dt = datetime.datetime.fromisoformat(v)
                return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)
            except Exception as exc:
                raise ValueError(f"Ungültiges Zeitformat für 'zeit': {exc}")
        if isinstance(v, datetime.datetime):
            return v if v.tzinfo else v.replace(tzinfo=datetime.timezone.utc)
        raise ValueError("Ungültiger Typ für 'zeit'")

class RouteOut(BaseModel):
    start: Optional[Coords] = None
    ziel: Optional[Coords] = None
    distance_m: Optional[float] = None
    duration_s: Optional[float] = None
    geometry: Optional[List[List[float]]] = None  # [[lat,lng], ...]
    bbox: Optional[List[float]] = None            # [minLat,minLng,maxLat,maxLng]

class AlarmOut(BaseModel):
    id: str
    agencyName: Optional[str] = None
    stichwort: str
    zeit: datetime.datetime
    adresse: Optional[str] = None
    infos: List[str] = []
    fleet: List[FleetItem] = []
    valid_until: datetime.datetime
    route: Optional[RouteOut] = None

# -----------------------------------------------------------------------------
# Storage Helpers
# -----------------------------------------------------------------------------
def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("Konnte %s nicht laden (%s). Verwende Default.", path, exc)
        return default

def _save_json(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)

def load_history() -> List[dict]:
    return _load_json(HISTORY_PATH, [])

def append_history(entry: dict):
    data = load_history()
    data.append(entry)
    data = data[-500:]
    _save_json(HISTORY_PATH, data)

def load_state() -> dict:
    return _load_json(STATE_PATH, {"current_alarm": None, "selected_sound": None})

def save_state(state: dict):
    _save_json(STATE_PATH, state)

# -----------------------------------------------------------------------------
# Sounds
# -----------------------------------------------------------------------------
def list_sounds() -> List[dict]:
    """Scanne STATIC_DIR nach kompatiblen Audiodateien und liefere Liste."""
    items: List[dict] = []
    if not os.path.isdir(STATIC_DIR):
        return items
    for name in sorted(os.listdir(STATIC_DIR)):
        p = os.path.join(STATIC_DIR, name)
        if not os.path.isfile(p):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in SOUND_EXTS:
            continue
        size = os.path.getsize(p)
        items.append({
            "filename": name,
            "url": f"/static/{name}",
            "size": size
        })
    return items

def pick_default_sound(sounds: List[dict]) -> Optional[str]:
    """Wähle eine sinnvolle Default-Sirene."""
    if not sounds:
        return None
    # 1) sirene.* bevorzugen
    for s in sounds:
        if "sirene" in s["filename"].lower():
            return s["filename"]
    # 2) feuerwehr.*
    for s in sounds:
        if "feuerwehr" in s["filename"].lower():
            return s["filename"]
    # 3) erste Datei
    return sounds[0]["filename"]

# -----------------------------------------------------------------------------
# Routing (OSRM)
# -----------------------------------------------------------------------------
def compute_osrm_route(start: Coords, ziel: Coords) -> RouteOut:
    params = {
        "overview": "full",
        "geometries": "geojson",
        "alternatives": "false",
        "steps": "false",
    }
    url = f"{OSRM_BASE_URL}/route/v1/driving/{start.lon},{start.lat};{ziel.lon},{ziel.lat}"
    try:
        resp = requests.get(
            url,
            params=params,
            timeout=OSRM_TIMEOUT,
            headers={"User-Agent": "feuerwehr-dashboard/1.0"},
        )
        resp.raise_for_status()
        js = resp.json()
        if js.get("code") != "Ok":
            raise RuntimeError(f"OSRM error: {js.get('code')}")
        r0 = js["routes"][0]
        coords = r0["geometry"]["coordinates"]  # [lon, lat]
        geometry = [[lat, lon] for lon, lat in coords]
        lats = [lat for lat, _ in geometry]
        lngs = [lng for _, lng in geometry]
        bbox = [min(lats), min(lngs), max(lats), max(lngs)]
        return RouteOut(
            start=start,
            ziel=ziel,
            distance_m=float(r0["distance"]),
            duration_s=float(r0["duration"]),
            geometry=geometry,
            bbox=bbox,
        )
    except Exception as exc:
        logger.warning("OSRM-Route fehlgeschlagen: %s", exc)
        return RouteOut(start=start, ziel=ziel)

# -----------------------------------------------------------------------------
# WebSocket Manager
# -----------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, message: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

manager = ConnectionManager()

# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

@app.get("/healthz")
async def healthz():
    st = load_state()
    return {"ok": True, "active": bool(st.get("current_alarm")), "clients": len(manager.active)}

@app.get("/dashboard-data")
async def dashboard_data():
    st = load_state()
    hist = load_history()
    return {"state": st, "history": hist[-50:]}

@app.get("/history")
async def history(limit: int = 25):
    hist = load_history()
    return hist[-max(1, min(limit, 200)):]

@app.get("/sounds")
async def sounds():
    st = load_state()
    items = list_sounds()
    selected = st.get("selected_sound")
    names = {i["filename"] for i in items}
    if not selected or selected not in names:
        selected = pick_default_sound(items)
        st["selected_sound"] = selected
        save_state(st)
    return {"selected": selected, "list": items}

@app.post("/sound")
async def set_sound(payload: dict):
    # erwartet {"filename": "xxx.mp3"}
    if "filename" not in payload:
        raise HTTPException(status_code=400, detail="filename fehlt")
    filename = os.path.basename(str(payload["filename"]))
    items = list_sounds()
    names = {i["filename"] for i in items}
    if filename not in names:
        raise HTTPException(status_code=404, detail="Datei nicht gefunden oder nicht erlaubt")
    st = load_state()
    st["selected_sound"] = filename
    save_state(st)
    # Push an Clients
    await manager.broadcast({"type": "sound", "payload": {"selected": filename, "url": f"/static/{filename}"}})
    return {"ok": True, "selected": filename}

@app.post("/alarm", response_model=AlarmOut)
async def post_alarm(alarm: AlarmIn):
    # Defaults
    if not alarm.agencyName:
        alarm.agencyName = DEFAULT_AGENCY_NAME

    valid_minutes = alarm.valid_minutes or VALID_MINUTES_DEFAULT
    if valid_minutes <= 0:
        raise HTTPException(status_code=400, detail="valid_minutes muss > 0 sein")

    alarm_time = alarm.zeit
    valid_until = alarm_time + datetime.timedelta(minutes=valid_minutes)

    if alarm.ziel and not alarm.start:
        alarm.start = Coords(lat=DEFAULT_START_LAT, lon=DEFAULT_START_LON)

    route: Optional[RouteOut] = None
    if alarm.start and alarm.ziel:
        loop = asyncio.get_running_loop()
        route = await loop.run_in_executor(None, compute_osrm_route, alarm.start, alarm.ziel)

    out = AlarmOut(
        id=str(uuid.uuid4()),
        agencyName=alarm.agencyName,
        stichwort=alarm.stichwort,
        zeit=alarm_time,
        adresse=alarm.adresse,
        infos=alarm.infos,
        fleet=alarm.fleet,
        valid_until=valid_until,
        route=route,
    )

    state = load_state()
    state["current_alarm"] = json.loads(out.model_dump_json())
    save_state(state)

    append_history({
        "id": out.id,
        "stichwort": out.stichwort,
        "zeit": out.zeit.isoformat(),
        "adresse": out.adresse,
        "distance_m": out.route.distance_m if out.route else None,
        "duration_s": out.route.duration_s if out.route else None,
    })

    try:
        await manager.broadcast({"type": "alarm", "payload": json.loads(out.model_dump_json())})
    except Exception as exc:
        logger.warning("Broadcast fehlgeschlagen: %s", exc)

    async def expire_task(alarm_id: str, when: datetime.datetime):
        now = datetime.datetime.now(datetime.timezone.utc)
        delay = max(0.0, (when - now).total_seconds())
        await asyncio.sleep(delay)
        current = load_state().get("current_alarm")
        if current and current.get("id") == alarm_id:
            save_state({"current_alarm": None, "selected_sound": state.get("selected_sound")})
            hist = load_history()
            await manager.broadcast({"type": "idle", "payload": {"last_history": hist[-25:]}})

    asyncio.create_task(expire_task(out.id, valid_until))
    return out

@app.post("/expire-now")
async def expire_now():
    st = load_state()
    save_state({"current_alarm": None, "selected_sound": st.get("selected_sound")})
    hist = load_history()
    await manager.broadcast({"type": "idle", "payload": {"last_history": hist[-25:]}})
    return {"ok": True}

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        st = load_state()
        hist = load_history()
        await ws.send_json({"type": "hello", "payload": {"state": st, "history": hist[-25:]}})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception as exc:
        logger.warning("WebSocket Fehler: %s", exc)
        manager.disconnect(ws)

# -----------------------------------------------------------------------------
# Globale Fehlerantwort (Fallback)
# -----------------------------------------------------------------------------
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unbehandelte Ausnahme: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "Interner Serverfehler"})
