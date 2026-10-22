"""Maintenance (Einsätze anlegen, planen, Einstellungen) – Port 8081, passwortgeschützt."""
from __future__ import annotations

import asyncio
import datetime
import logging
import os
import time
from typing import Any, List, Optional

import requests
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..common import auth, store
from ..common.models import AlarmIn
from ..common.store import VEHICLE_STATUSES, load_config, new_id, update_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("feuerwehr")

DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://127.0.0.1:8080").rstrip("/")
TOKEN = store.get_secrets()["internal_token"]
MISSED_AFTER_S = 120          # geplanter Einsatz, der > 2 min überfällig ist, wird nicht mehr ausgelöst
UTC = datetime.timezone.utc

app = FastAPI(title="Feuerwehr Maintenance", version="2.0.0")
app.mount("/static", StaticFiles(directory=store.WEB_DIR), name="static")


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(UTC)


# -----------------------------------------------------------------------------
# Auth
# -----------------------------------------------------------------------------
def require_login(request: Request):
    if not auth.check_cookie(request.cookies.get(auth.COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="Nicht angemeldet")


_fails: list[float] = []


class LoginIn(BaseModel):
    password: str


@app.post("/api/login")
async def login(body: LoginIn, response: Response):
    recent = [t for t in _fails if time.time() - t < 60]
    _fails[:] = recent
    if len(recent) >= 10:
        raise HTTPException(status_code=429, detail="Zu viele Versuche – bitte kurz warten.")
    if not auth.verify_password(body.password):
        _fails.append(time.time())
        await asyncio.sleep(1)
        raise HTTPException(status_code=401, detail="Falsches Passwort")
    response.set_cookie(auth.COOKIE_NAME, auth.make_cookie(), max_age=auth.COOKIE_MAX_AGE,
                        httponly=True, samesite="lax", path="/")
    return {"ok": True}


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/me")
async def me(request: Request):
    ok = auth.check_cookie(request.cookies.get(auth.COOKIE_NAME))
    return {"authenticated": ok, "default_password": auth.uses_default_password() if ok else None,
            "settings": store.public_settings() if ok else None}


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(os.path.join(store.WEB_DIR, "maintenance", "index.html"))


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# -----------------------------------------------------------------------------
# Dashboard-Anbindung
# -----------------------------------------------------------------------------
def dash(method: str, path: str, **kw) -> Any:
    try:
        r = requests.request(method, DASHBOARD_URL + path, headers={"X-API-Key": TOKEN}, timeout=15, **kw)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Dashboard nicht erreichbar: {exc}")
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:
            detail = r.text
        raise HTTPException(status_code=r.status_code if r.status_code < 500 else 502, detail=detail)
    return r.json()


def build_alarm(p: "AlarmPayload", cfg: dict) -> dict:
    """Formular-Payload -> Dashboard-AlarmIn."""
    vehicles = {v["id"]: v for v in cfg["vehicles"]}
    fleet = []
    for i, item in enumerate(p.fleet, start=1):
        name = item.name or vehicles.get(item.vehicle_id or "", {}).get("name") or item.vehicle_id
        fleet.append({"order": i, "name": name, "status": item.status, "vehicle_id": item.vehicle_id})
    body = {
        "stichwort": p.stichwort, "adresse": p.adresse or None,
        "infos": [t.strip() for t in p.infos if t.strip()],
        "valid_minutes": p.valid_minutes or cfg["settings"]["valid_minutes_default"],
        "fleet": fleet, "exercise": p.exercise, "play_sound": p.play_sound,
    }
    if p.lat is not None and p.lon is not None:
        body["ziel"] = {"lat": p.lat, "lon": p.lon}
    return body


class FleetIn(BaseModel):
    vehicle_id: Optional[str] = None
    name: Optional[str] = None
    status: str = "alarmiert"


class AlarmPayload(BaseModel):
    stichwort: str = Field(min_length=1)
    adresse: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    infos: List[str] = []
    valid_minutes: Optional[int] = None
    fleet: List[FleetIn] = []
    exercise: bool = True
    play_sound: bool = True


class SendIn(BaseModel):
    payload: AlarmPayload
    mode: str = "now"                      # now | in | at
    minutes: Optional[float] = None        # mode=in
    at: Optional[datetime.datetime] = None  # mode=at (mit Zeitzone, vom Browser als ISO/UTC)


@app.get("/api/meta", dependencies=[Depends(require_login)])
async def meta():
    cfg = load_config()
    return {
        "statuses": VEHICLE_STATUSES, "sounds": store.list_sounds(),
        "effective_sound": store.effective_sound(cfg["settings"]["sound"]),
        "settings": cfg["settings"], "vehicles": cfg["vehicles"], "templates": cfg["templates"],
    }


# -----------------------------------------------------------------------------
# Einsatz senden / planen
# -----------------------------------------------------------------------------
@app.post("/api/send", dependencies=[Depends(require_login)])
async def send(body: SendIn):
    cfg = load_config()
    alarm = build_alarm(body.payload, cfg)
    if body.mode == "now":
        out = await asyncio.get_running_loop().run_in_executor(None, lambda: dash("POST", "/api/alarm", json=alarm))
        return {"sent": True, "alarm": out}
    if body.mode == "in":
        if not body.minutes or body.minutes <= 0:
            raise HTTPException(status_code=400, detail="Minuten müssen > 0 sein")
        at = now_utc() + datetime.timedelta(minutes=body.minutes)
    elif body.mode == "at":
        if not body.at:
            raise HTTPException(status_code=400, detail="Uhrzeit fehlt")
        at = body.at if body.at.tzinfo else body.at.replace(tzinfo=UTC)
        if at <= now_utc():
            raise HTTPException(status_code=400, detail="Zeitpunkt liegt in der Vergangenheit")
    else:
        raise HTTPException(status_code=400, detail="Unbekannter Modus")
    entry = {"id": new_id(), "created": now_utc().isoformat(), "at": at.astimezone(UTC).isoformat(),
             "status": "pending", "stichwort": alarm["stichwort"], "adresse": alarm.get("adresse"),
             "alarm": alarm}
    update_config(lambda c: c["schedules"].append(entry))
    return {"sent": False, "scheduled": entry}


@app.get("/api/schedules", dependencies=[Depends(require_login)])
async def schedules():
    items = load_config()["schedules"]
    return sorted(items, key=lambda s: s["at"])


def _find(cfg: dict, sid: str) -> dict:
    for s in cfg["schedules"]:
        if s["id"] == sid:
            return s
    raise HTTPException(status_code=404, detail="Eintrag nicht gefunden")


@app.post("/api/schedules/{sid}/cancel", dependencies=[Depends(require_login)])
async def cancel_schedule(sid: str):
    def fn(cfg):
        s = _find(cfg, sid)
        if s["status"] != "pending":
            raise HTTPException(status_code=409, detail=f"Status ist bereits '{s['status']}'")
        s["status"] = "cancelled"
    update_config(fn)
    return {"ok": True}


@app.post("/api/schedules/{sid}/fire", dependencies=[Depends(require_login)])
async def fire_schedule(sid: str):
    cfg = load_config()
    s = _find(cfg, sid)
    if s["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Status ist bereits '{s['status']}'")
    await asyncio.get_running_loop().run_in_executor(None, _fire, sid)
    return {"ok": True}


@app.delete("/api/schedules", dependencies=[Depends(require_login)])
async def prune_schedules():
    """Erledigte/abgebrochene Einträge entfernen."""
    update_config(lambda c: c.__setitem__("schedules", [s for s in c["schedules"] if s["status"] == "pending"]))
    return {"ok": True}


def _fire(sid: str) -> None:
    cfg = load_config()
    s = _find(cfg, sid)
    try:
        dash("POST", "/api/alarm", json=s["alarm"])
        result: dict = {"status": "sent", "sent_at": now_utc().isoformat()}
    except HTTPException as exc:
        logger.warning("Geplanter Einsatz %s fehlgeschlagen: %s", sid, exc.detail)
        result = {"status": "failed", "error": str(exc.detail)}

    def fn(c):
        _find(c, sid).update(result)
    update_config(fn)


async def scheduler():
    while True:
        try:
            await asyncio.sleep(1)
            now = now_utc()
            for s in load_config()["schedules"]:
                if s["status"] != "pending":
                    continue
                at = datetime.datetime.fromisoformat(s["at"])
                if at > now:
                    continue
                if (now - at).total_seconds() > MISSED_AFTER_S:
                    update_config(lambda c, sid=s["id"]: _find(c, sid).update(
                        status="missed", error="Zeitpunkt verpasst (Dienst war nicht aktiv)"))
                else:
                    await asyncio.get_running_loop().run_in_executor(None, _fire, s["id"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Scheduler-Fehler: %s", exc)


@app.on_event("startup")
async def _startup():
    if auth.uses_default_password():
        logger.warning("ACHTUNG: Standard-Passwort '%s' aktiv – bitte MAINTENANCE_PASSWORD setzen "
                       "oder in den Einstellungen ändern.", auth.DEFAULT_PASSWORD)
    asyncio.create_task(scheduler())


# -----------------------------------------------------------------------------
# Live-Einsatz (Proxy zum Dashboard)
# -----------------------------------------------------------------------------
def _dash_get(path: str):
    try:
        return requests.get(DASHBOARD_URL + path, timeout=5).json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Dashboard nicht erreichbar: {exc}")


@app.get("/api/live", dependencies=[Depends(require_login)])
async def live():
    return await asyncio.get_running_loop().run_in_executor(None, _dash_get, "/api/state")


class StatusIn(BaseModel):
    order: int
    status: str


@app.post("/api/live/vehicle-status", dependencies=[Depends(require_login)])
async def live_vehicle_status(body: StatusIn):
    return await asyncio.get_running_loop().run_in_executor(
        None, lambda: dash("POST", "/api/vehicle-status", json=body.model_dump()))


@app.post("/api/live/end", dependencies=[Depends(require_login)])
async def live_end():
    return await asyncio.get_running_loop().run_in_executor(None, lambda: dash("POST", "/api/expire-now"))


@app.get("/api/history", dependencies=[Depends(require_login)])
async def history():
    return list(reversed(store.load_history()[-100:]))


@app.delete("/api/history", dependencies=[Depends(require_login)])
async def history_clear():
    return await asyncio.get_running_loop().run_in_executor(None, lambda: dash("DELETE", "/api/history"))


# -----------------------------------------------------------------------------
# Geocoding (Nominatim) – braucht Internet, Koordinaten lassen sich auch manuell setzen
# -----------------------------------------------------------------------------
@app.get("/api/geocode", dependencies=[Depends(require_login)])
async def geocode(q: str):
    def run():
        r = requests.get("https://nominatim.openstreetmap.org/search",
                         params={"q": q, "format": "jsonv2", "limit": 5, "countrycodes": "de", "accept-language": "de"},
                         headers={"User-Agent": "feuerwehr-dashboard/2.0"}, timeout=8)
        r.raise_for_status()
        return [{"label": x["display_name"], "lat": float(x["lat"]), "lon": float(x["lon"])} for x in r.json()]
    try:
        return await asyncio.get_running_loop().run_in_executor(None, run)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Adresssuche nicht möglich (Internet?): {exc}")


# -----------------------------------------------------------------------------
# Fahrzeuge
# -----------------------------------------------------------------------------
class VehicleIn(BaseModel):
    name: str = Field(min_length=1)
    kind: str = ""
    active: bool = True


@app.post("/api/vehicles", dependencies=[Depends(require_login)])
async def vehicle_create(v: VehicleIn):
    item = {"id": new_id(), **v.model_dump()}
    update_config(lambda c: c["vehicles"].append(item))
    return item


@app.put("/api/vehicles/{vid}", dependencies=[Depends(require_login)])
async def vehicle_update(vid: str, v: VehicleIn):
    def fn(c):
        for x in c["vehicles"]:
            if x["id"] == vid:
                x.update(v.model_dump())
                return x
        raise HTTPException(status_code=404, detail="Fahrzeug nicht gefunden")
    return update_config(fn)


@app.delete("/api/vehicles/{vid}", dependencies=[Depends(require_login)])
async def vehicle_delete(vid: str):
    def fn(c):
        c["vehicles"] = [x for x in c["vehicles"] if x["id"] != vid]
        for t in c["templates"]:
            t["vehicle_ids"] = [i for i in t["vehicle_ids"] if i != vid]
    update_config(fn)
    return {"ok": True}


# -----------------------------------------------------------------------------
# Vorlagen
# -----------------------------------------------------------------------------
class TemplateIn(BaseModel):
    name: str = Field(min_length=1)
    stichwort: str = ""
    adresse: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    infos: List[str] = []
    vehicle_ids: List[str] = []
    valid_minutes: Optional[int] = None
    exercise: bool = True


@app.post("/api/templates", dependencies=[Depends(require_login)])
async def template_create(t: TemplateIn):
    item = {"id": new_id(), **t.model_dump()}
    update_config(lambda c: c["templates"].append(item))
    return item


@app.put("/api/templates/{tid}", dependencies=[Depends(require_login)])
async def template_update(tid: str, t: TemplateIn):
    def fn(c):
        for x in c["templates"]:
            if x["id"] == tid:
                x.update(t.model_dump())
                return x
        raise HTTPException(status_code=404, detail="Vorlage nicht gefunden")
    return update_config(fn)


@app.delete("/api/templates/{tid}", dependencies=[Depends(require_login)])
async def template_delete(tid: str):
    update_config(lambda c: c.__setitem__("templates", [x for x in c["templates"] if x["id"] != tid]))
    return {"ok": True}


# -----------------------------------------------------------------------------
# Einstellungen
# -----------------------------------------------------------------------------
class StationIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class SettingsIn(BaseModel):
    theme_mode: str = Field(pattern="^(auto|light|dark)$")
    agency_name: str = Field(min_length=1)
    station: StationIn
    valid_minutes_default: int = Field(gt=0, le=1440)
    sound: Optional[str] = None
    weather_enabled: bool = True


@app.put("/api/settings", dependencies=[Depends(require_login)])
async def settings_update(s: SettingsIn):
    if s.sound and s.sound not in {x["filename"] for x in store.list_sounds()}:
        raise HTTPException(status_code=400, detail="Unbekannte Sound-Datei")
    update_config(lambda c: c["settings"].update(s.model_dump()))
    return load_config()["settings"]


class ThemeIn(BaseModel):
    theme_mode: str = Field(pattern="^(auto|light|dark)$")


@app.put("/api/settings/theme", dependencies=[Depends(require_login)])
async def theme_update(t: ThemeIn):
    update_config(lambda c: c["settings"].update(theme_mode=t.theme_mode))
    return {"theme_mode": t.theme_mode}


class PasswordIn(BaseModel):
    current: str
    new: str = Field(min_length=6)


@app.post("/api/password")
async def change_password(body: PasswordIn, request: Request, response: Response):
    require_login(request)
    if not auth.verify_password(body.current):
        raise HTTPException(status_code=403, detail="Aktuelles Passwort ist falsch")
    update_config(lambda c: c["auth"].update(password_hash=auth.hash_password(body.new)))
    response.set_cookie(auth.COOKIE_NAME, auth.make_cookie(), max_age=auth.COOKIE_MAX_AGE,
                        httponly=True, samesite="lax", path="/")
    return {"ok": True}


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unbehandelte Ausnahme: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "Interner Serverfehler"})
