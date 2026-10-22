"""Routing (OSRM mit Luftlinien-Fallback) und Wetter (Open-Meteo, gecacht)."""
from __future__ import annotations

import logging
import math
import os
import time

import requests

from ..common.models import Coords, RouteOut

logger = logging.getLogger("feuerwehr")

OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org")
OSRM_TIMEOUT = float(os.environ.get("OSRM_TIMEOUT", "8.0"))
UA = {"User-Agent": "feuerwehr-dashboard/2.0"}


def haversine_m(a: Coords, b: Coords) -> float:
    r = 6371000.0
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dphi, dl = p2 - p1, math.radians(b.lon - a.lon)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def fallback_route(start: Coords, ziel: Coords, speed_kmh: float | None) -> RouteOut:
    """Luftlinie, wenn OSRM nicht erreichbar ist (z. B. kein Internet)."""
    dist = haversine_m(start, ziel) * 1.3          # grober Umwegfaktor
    speed = (speed_kmh or 50.0) / 3.6
    geometry = [[start.lat, start.lon], [ziel.lat, ziel.lon]]
    return RouteOut(
        start=start, ziel=ziel, distance_m=dist, duration_s=dist / speed, geometry=geometry,
        bbox=[min(start.lat, ziel.lat), min(start.lon, ziel.lon), max(start.lat, ziel.lat), max(start.lon, ziel.lon)],
        approximate=True,
    )


def compute_route(start: Coords, ziel: Coords, speed_kmh: float | None = None) -> RouteOut:
    url = f"{OSRM_BASE_URL}/route/v1/driving/{start.lon},{start.lat};{ziel.lon},{ziel.lat}"
    params = {"overview": "full", "geometries": "geojson", "alternatives": "false", "steps": "false"}
    try:
        resp = requests.get(url, params=params, timeout=OSRM_TIMEOUT, headers=UA)
        resp.raise_for_status()
        js = resp.json()
        if js.get("code") != "Ok":
            raise RuntimeError(f"OSRM error: {js.get('code')}")
        r0 = js["routes"][0]
        geometry = [[lat, lon] for lon, lat in r0["geometry"]["coordinates"]]
        lats, lngs = [p[0] for p in geometry], [p[1] for p in geometry]
        return RouteOut(
            start=start, ziel=ziel, distance_m=float(r0["distance"]), duration_s=float(r0["duration"]),
            geometry=geometry, bbox=[min(lats), min(lngs), max(lats), max(lngs)],
        )
    except Exception as exc:
        logger.warning("OSRM-Route fehlgeschlagen (%s) – verwende Luftlinie.", exc)
        return fallback_route(start, ziel, speed_kmh)


# -----------------------------------------------------------------------------
# Wetter
# -----------------------------------------------------------------------------
WEATHER_TTL = 600
_weather: dict = {"key": None, "ts": 0.0, "data": None}

# WMO-Code -> (Text, Symbol)
_WMO = {
    0: ("Klar", "☀️"), 1: ("Überwiegend klar", "🌤️"), 2: ("Teils bewölkt", "⛅"), 3: ("Bedeckt", "☁️"),
    45: ("Nebel", "🌫️"), 48: ("Reifnebel", "🌫️"),
    51: ("Leichter Niesel", "🌦️"), 53: ("Niesel", "🌦️"), 55: ("Starker Niesel", "🌧️"),
    56: ("Gefrierender Niesel", "🌧️"), 57: ("Gefrierender Niesel", "🌧️"),
    61: ("Leichter Regen", "🌦️"), 63: ("Regen", "🌧️"), 65: ("Starker Regen", "🌧️"),
    66: ("Gefrierender Regen", "🌧️"), 67: ("Gefrierender Regen", "🌧️"),
    71: ("Leichter Schnee", "🌨️"), 73: ("Schnee", "🌨️"), 75: ("Starker Schnee", "❄️"), 77: ("Schneegriesel", "🌨️"),
    80: ("Regenschauer", "🌦️"), 81: ("Regenschauer", "🌧️"), 82: ("Starke Schauer", "⛈️"),
    85: ("Schneeschauer", "🌨️"), 86: ("Starke Schneeschauer", "❄️"),
    95: ("Gewitter", "⛈️"), 96: ("Gewitter mit Hagel", "⛈️"), 99: ("Gewitter mit Hagel", "⛈️"),
}
_DIRS = ["N", "NO", "O", "SO", "S", "SW", "W", "NW"]


def get_weather(lat: float, lon: float) -> dict:
    key = f"{lat:.3f},{lon:.3f}"
    now = time.time()
    if _weather["key"] == key and _weather["data"] and now - _weather["ts"] < WEATHER_TTL:
        return _weather["data"]
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": lat, "longitude": lon, "timezone": "auto",
                    "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,wind_direction_10m,precipitation,relative_humidity_2m"},
            timeout=6, headers=UA,
        )
        r.raise_for_status()
        c = r.json()["current"]
        text, icon = _WMO.get(int(c.get("weather_code", 0)), ("–", "🌡️"))
        data = {
            "ok": True, "stale": False, "temp": round(c["temperature_2m"], 1),
            "feels_like": round(c.get("apparent_temperature", c["temperature_2m"]), 1),
            "text": text, "icon": icon, "wind_kmh": round(c["wind_speed_10m"]),
            "wind_dir": _DIRS[int((c.get("wind_direction_10m", 0) % 360) / 45 + 0.5) % 8],
            "precip_mm": c.get("precipitation", 0), "humidity": c.get("relative_humidity_2m"),
            "updated": time.strftime("%H:%M"),
        }
        _weather.update(key=key, ts=now, data=data)
        return data
    except Exception as exc:
        logger.warning("Wetter nicht abrufbar: %s", exc)
        if _weather["data"]:
            return {**_weather["data"], "stale": True}
        return {"ok": False}
