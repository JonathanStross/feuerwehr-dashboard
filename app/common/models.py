from __future__ import annotations

import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class Coords(BaseModel):
    lat: float
    lon: float


class FleetItem(BaseModel):
    order: int
    name: str
    status: str = "alarmiert"
    vehicle_id: Optional[str] = None


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
    exercise: bool = False          # Übung/fiktiv -> Badge im Dashboard
    play_sound: bool = True

    @field_validator("zeit", mode="before")
    @classmethod
    def parse_dt(cls, v):
        utc = datetime.timezone.utc
        if v is None or v == "":
            return datetime.datetime.now(utc)
        if isinstance(v, (int, float)):
            return datetime.datetime.fromtimestamp(v, tz=utc)
        if isinstance(v, str):
            try:
                dt = datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
            except Exception as exc:
                raise ValueError(f"Ungültiges Zeitformat für 'zeit': {exc}")
            return dt if dt.tzinfo else dt.replace(tzinfo=utc)
        if isinstance(v, datetime.datetime):
            return v if v.tzinfo else v.replace(tzinfo=utc)
        raise ValueError("Ungültiger Typ für 'zeit'")


class RouteOut(BaseModel):
    start: Optional[Coords] = None
    ziel: Optional[Coords] = None
    distance_m: Optional[float] = None
    duration_s: Optional[float] = None
    geometry: Optional[List[List[float]]] = None  # [[lat,lng], ...]
    bbox: Optional[List[float]] = None            # [minLat,minLng,maxLat,maxLng]
    approximate: bool = False                     # True = Luftlinie (OSRM nicht erreichbar)


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
    exercise: bool = False
    play_sound: bool = True
