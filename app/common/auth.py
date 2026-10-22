"""Passwortschutz für die Maintenance-Seite mit dauerhaftem Login-Cookie."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time

from .store import get_secrets, load_config

logger = logging.getLogger("feuerwehr")

COOKIE_NAME = "fw_session"
COOKIE_MAX_AGE = 10 * 365 * 24 * 3600     # ~10 Jahre: einmal anmelden, läuft dauerhaft
DEFAULT_PASSWORD = "feuerwehr"
_PBKDF_ROUNDS = 200_000


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF_ROUNDS)
    return f"pbkdf2${salt.hex()}${dk.hex()}"


def _check_hash(password: str, stored: str) -> bool:
    try:
        _, salt_hex, dk_hex = stored.split("$")
        cand = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), _PBKDF_ROUNDS)
        return hmac.compare_digest(cand.hex(), dk_hex)
    except Exception:
        return False


def _env_password() -> str:
    pw = os.environ.get("MAINTENANCE_PASSWORD") or DEFAULT_PASSWORD
    return pw


def uses_default_password() -> bool:
    return not load_config()["auth"]["password_hash"] and _env_password() == DEFAULT_PASSWORD


def verify_password(password: str) -> bool:
    stored = load_config()["auth"]["password_hash"]
    if stored:
        return _check_hash(password, stored)
    return hmac.compare_digest(password.encode(), _env_password().encode())


def _fingerprint() -> str:
    """Ändert sich mit dem Passwort -> alte Cookies werden ungültig."""
    stored = load_config()["auth"]["password_hash"] or "env:" + _env_password()
    return hashlib.sha256(stored.encode()).hexdigest()[:16]


def _sign(payload: str) -> str:
    key = get_secrets()["session_secret"].encode()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def make_cookie() -> str:
    body = base64.urlsafe_b64encode(json.dumps({"iat": int(time.time()), "fp": _fingerprint()}).encode()).decode()
    return f"{body}.{_sign(body)}"


def check_cookie(value: str | None) -> bool:
    if not value or "." not in value:
        return False
    body, sig = value.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(body)):
        return False
    try:
        data = json.loads(base64.urlsafe_b64decode(body.encode()))
    except Exception:
        return False
    return data.get("fp") == _fingerprint()
