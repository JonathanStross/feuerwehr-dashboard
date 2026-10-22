"""Dashboard (Anzeige) – Port 8080. Empfängt Alarme per API und pusht sie per WebSocket."""
from __future__ import annotations

import asyncio
import datetime
import hmac
import json
import logging
import os
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..common import store
from ..common.models import AlarmIn, AlarmOut, Coords, RouteOut
from ..common.store import VEHICLE_STATUSES
from . import services

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("feuerwehr")

app = FastAPI(title="Feuerwehr Dashboard", version="2.0.0")
app.mount("/static", StaticFiles(directory=store.WEB_DIR), name="static")

TOKEN = store.get_secrets()["internal_token"]
UTC = datetime.timezone.utc


def require_token(x_api_key: str = Header(default="")):
    if not hmac.compare_digest(x_api_key.encode(), TOKEN.encode()):
        raise HTTPException(status_code=401, detail="Ungültiger API-Key")


# -----------------------------------------------------------------------------
# WebSocket
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
        for ws in list(self.active):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


# -----------------------------------------------------------------------------
# Helfer
# -----------------------------------------------------------------------------
def settings_payload() -> dict:
    s = store.public_settings()
    sound = store.effective_sound(s["sound"])
    s["sound_url"] = f"/static/sounds/{sound}" if sound else None
    s["statuses"] = VEHICLE_STATUSES
    return s


def active_alarm() -> Optional[dict]:
    """Aktueller Alarm – abgelaufene Alarme gelten als beendet (auch nach Neustart)."""
    alarm = store.load_state().get("current_alarm")
    if not alarm:
        return None
    try:
        if datetime.datetime.fromisoformat(alarm["valid_until"].replace("Z", "+00:00")) <= datetime.datetime.now(UTC):
            return None
    except Exception:
        pass
    return alarm


async def go_idle():
    store.save_state({"current_alarm": None})
    await manager.broadcast({"type": "idle", "payload": {"last_history": store.load_history()[-50:]}})


# -----------------------------------------------------------------------------
# Seiten / lesende Endpunkte (offen, nur im lokalen Netz gedacht)
# -----------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(os.path.join(store.WEB_DIR, "dashboard", "index.html"))


@app.get("/healthz")
async def healthz():
    return {"ok": True, "active": bool(active_alarm()), "clients": len(manager.active)}


@app.get("/api/settings")
async def api_settings():
    return settings_payload()


@app.get("/api/state")
async def api_state():
    return {"current_alarm": active_alarm(), "history": store.load_history()[-50:]}


@app.get("/history")
async def history(limit: int = 25):
    return store.load_history()[-max(1, min(limit, 200)):]


@app.get("/api/weather")
async def api_weather():
    s = store.load_config()["settings"]
    if not s["weather_enabled"]:
        return {"ok": False, "disabled": True}
    st = s["station"]
    return await asyncio.get_running_loop().run_in_executor(None, services.get_weather, st["lat"], st["lon"])


# -----------------------------------------------------------------------------
# Schreibende Endpunkte (nur mit API-Key – die Maintenance-App kennt ihn)
# -----------------------------------------------------------------------------
@app.post("/api/alarm", response_model=AlarmOut, dependencies=[Depends(require_token)])
@app.post("/alarm", response_model=AlarmOut, include_in_schema=False, dependencies=[Depends(require_token)])
async def post_alarm(alarm: AlarmIn):
    cfg = store.load_config()
    s = cfg["settings"]
    valid_minutes = alarm.valid_minutes or s["valid_minutes_default"]
    if valid_minutes <= 0:
        raise HTTPException(status_code=400, detail="valid_minutes muss > 0 sein")

    start = alarm.start or (Coords(**s["station"]) if alarm.ziel else None)
    route: Optional[RouteOut] = None
    if start and alarm.ziel:
        route = await asyncio.get_running_loop().run_in_executor(
            None, services.compute_route, start, alarm.ziel, alarm.route_speed_kmh)

    out = AlarmOut(
        id=store.new_id(), agencyName=alarm.agencyName or s["agency_name"], stichwort=alarm.stichwort,
        zeit=alarm.zeit, adresse=alarm.adresse, infos=alarm.infos, fleet=alarm.fleet,
        valid_until=alarm.zeit + datetime.timedelta(minutes=valid_minutes), route=route,
        exercise=alarm.exercise, play_sound=alarm.play_sound,
    )
    payload = json.loads(out.model_dump_json())
    store.save_state({"current_alarm": payload})
    store.append_history({
        "id": out.id, "stichwort": out.stichwort, "zeit": out.zeit.isoformat(), "adresse": out.adresse,
        "distance_m": out.route.distance_m if out.route else None,
        "duration_s": out.route.duration_s if out.route else None,
        "infos": out.infos, "exercise": out.exercise,
        "ziel": json.loads(alarm.ziel.model_dump_json()) if alarm.ziel else None,
        "fleet": [{"name": f.name, "vehicle_id": f.vehicle_id, "order": f.order} for f in out.fleet],
        "valid_minutes": valid_minutes,
    })
    await manager.broadcast({"type": "alarm", "payload": payload})
    return out


class VehicleStatusIn(BaseModel):
    order: int
    status: str


@app.post("/api/vehicle-status", dependencies=[Depends(require_token)])
async def vehicle_status(body: VehicleStatusIn):
    state = store.load_state()
    alarm = active_alarm()
    if not alarm:
        raise HTTPException(status_code=409, detail="Kein aktiver Einsatz")
    if body.status not in {s["key"] for s in VEHICLE_STATUSES}:
        raise HTTPException(status_code=400, detail="Unbekannter Status")
    hit = [f for f in alarm["fleet"] if f["order"] == body.order]
    if not hit:
        raise HTTPException(status_code=404, detail="Fahrzeug nicht im Einsatz")
    for f in hit:
        f["status"] = body.status
    state["current_alarm"] = alarm
    store.save_state(state)
    await manager.broadcast({"type": "fleet", "payload": {"id": alarm["id"], "fleet": alarm["fleet"]}})
    return {"ok": True, "fleet": alarm["fleet"]}


@app.post("/api/expire-now", dependencies=[Depends(require_token)])
@app.post("/expire-now", include_in_schema=False, dependencies=[Depends(require_token)])
async def expire_now():
    await go_idle()
    return {"ok": True}


@app.delete("/api/history", dependencies=[Depends(require_token)])
async def delete_history():
    store.clear_history()
    if not active_alarm():
        await manager.broadcast({"type": "idle", "payload": {"last_history": []}})
    return {"ok": True}


# -----------------------------------------------------------------------------
# WebSocket-Endpunkt
# -----------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        await ws.send_json({"type": "hello", "payload": {
            "state": {"current_alarm": active_alarm()},
            "history": store.load_history()[-50:],
            "settings": settings_payload(),
        }})
        while True:
            msg = await ws.receive_text()        # Clients senden "ping" als Heartbeat
            if msg == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("WebSocket Fehler: %s", exc)
    finally:
        manager.disconnect(ws)


# -----------------------------------------------------------------------------
# Hintergrund: Alarm-Ablauf + Einstellungs-Änderungen aus der Maintenance
# -----------------------------------------------------------------------------
async def watcher():
    last_cfg = store.config_mtime()
    while True:
        try:
            await asyncio.sleep(1)
            raw = store.load_state().get("current_alarm")
            if raw and not active_alarm():
                logger.info("Alarm %s abgelaufen.", raw.get("id"))
                await go_idle()
            mt = store.config_mtime()
            if mt != last_cfg:
                last_cfg = mt
                await manager.broadcast({"type": "settings", "payload": settings_payload()})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Watcher-Fehler: %s", exc)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(watcher())


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unbehandelte Ausnahme: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "Interner Serverfehler"})
