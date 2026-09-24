"""Local session persistence: device session, cached balance, pending debits.

The server is the source of truth. This file only avoids a network
round-trip on every hotkey and survives restarts.

Secrets: the long-lived refresh token is stored in the Windows
Credential Manager (via `keyring`), never in the JSON file. The 15-min
access token is kept in memory only. If keyring is unavailable the
refresh token falls back to the session file (logged as a warning).
"""
import json
import logging
import os
import threading
import time
import uuid
from typing import Optional

import config

log = logging.getLogger("session")
_lock = threading.Lock()

try:
    import keyring  # type: ignore
except Exception:  # pragma: no cover
    keyring = None

_KEYRING_USER = "refresh_token"

_state = {
    "client_device_id": None,  # stable per-install id (not a secret)
    "device_id": None,         # server device session id
    "user": None,              # {"id","full_name","email","role"}
    "tokens": None,            # int, cached questions remaining
    "balance": None,           # last full balance payload from the server
    "balance_synced_at": None, # monotonic-independent wall time of last sync (epoch s)
    "pending_consumes": [],    # [{"idempotency_key", "amount"}] not yet acknowledged
    "notified": {},            # {"low": int, "expiry": str} last warnings shown
    "refresh_token_fallback": None,
}

_access = {"token": None, "expires_at": 0.0}


# ---------- persistence ----------
def load() -> dict:
    with _lock:
        try:
            with open(config.SESSION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k in _state:
                if k in data:
                    _state[k] = data[k]
        except (FileNotFoundError, ValueError, OSError):
            pass
        if not _state.get("client_device_id"):
            _state["client_device_id"] = uuid.uuid4().hex
        _state["pending_consumes"] = list(_state.get("pending_consumes") or [])
        _state["notified"] = dict(_state.get("notified") or {})
    save()
    return get()


def save() -> None:
    with _lock:
        snapshot = dict(_state)
    try:
        os.makedirs(config.SESSION_DIR, exist_ok=True)
        tmp = config.SESSION_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f)
        os.replace(tmp, config.SESSION_FILE)  # atomic: never a half-written file
    except OSError:
        log.exception("[session] failed to persist session file")


# ---------- refresh token (secret) ----------
def get_refresh_token() -> Optional[str]:
    if keyring is not None:
        try:
            tok = keyring.get_password(config.KEYRING_SERVICE, _KEYRING_USER)
            if tok:
                return tok
        except Exception:
            log.warning("[session] keyring read failed; using file fallback")
    with _lock:
        return _state.get("refresh_token_fallback")


def _set_refresh_token(tok: Optional[str]) -> None:
    stored = False
    if keyring is not None:
        try:
            if tok:
                keyring.set_password(config.KEYRING_SERVICE, _KEYRING_USER, tok)
            else:
                try:
                    keyring.delete_password(config.KEYRING_SERVICE, _KEYRING_USER)
                except Exception:
                    pass
            stored = True
        except Exception:
            log.warning("[session] keyring write failed; using file fallback")
    with _lock:
        _state["refresh_token_fallback"] = None if stored else tok


# ---------- auth ----------
def set_device_session(payload: dict) -> None:
    """Store a /auth/verify-otp or /auth/refresh response."""
    _set_refresh_token(payload.get("refresh_token"))
    set_access(payload.get("access_token"), payload.get("access_expires_in") or 900)
    with _lock:
        if payload.get("device_id"):
            _state["device_id"] = payload["device_id"]
        if payload.get("user"):
            _state["user"] = payload["user"]
    if payload.get("balance"):
        set_balance(payload["balance"])
    else:
        save()


def set_access(token: Optional[str], expires_in: float) -> None:
    with _lock:
        _access["token"] = token
        # Renew 60s early so an in-flight request never carries a just-expired token.
        _access["expires_at"] = time.time() + max(0, float(expires_in) - 60)


def get_access_token() -> Optional[str]:
    with _lock:
        if _access["token"] and time.time() < _access["expires_at"]:
            return _access["token"]
        return None


def clear() -> None:
    _set_refresh_token(None)
    with _lock:
        cid = _state.get("client_device_id")
        for k in list(_state):
            _state[k] = [] if k == "pending_consumes" else ({} if k == "notified" else None)
        _state["client_device_id"] = cid  # keep install identity
        _access["token"] = None
        _access["expires_at"] = 0.0
    save()


def get() -> dict:
    with _lock:
        d = dict(_state)
    d.pop("refresh_token_fallback", None)
    return d


def is_authenticated() -> bool:
    return bool(get_refresh_token())


def client_device_id() -> str:
    with _lock:
        return _state["client_device_id"]


# ---------- balance cache ----------
def set_balance(balance: dict) -> None:
    with _lock:
        _state["balance"] = balance
        _state["balance_synced_at"] = time.time()
        server_n = int(balance.get("questions_remaining", balance.get("tokens", 0)))
        # Keep optimistic debits that the server hasn't acknowledged yet.
        pending = sum(int(p.get("amount", 0)) for p in _state["pending_consumes"])
        _state["tokens"] = max(0, server_n - pending)
    save()


def get_balance() -> Optional[dict]:
    with _lock:
        return _state.get("balance")


def get_tokens() -> Optional[int]:
    with _lock:
        return _state.get("tokens")


def set_tokens(n: Optional[int]) -> None:
    with _lock:
        _state["tokens"] = None if n is None else int(n)
    save()


# ---------- offline-safe debits ----------
def queue_consume(amount: int) -> dict:
    """Optimistic local debit + durable record of the pending server debit."""
    item = {"idempotency_key": uuid.uuid4().hex, "amount": int(amount)}
    with _lock:
        _state["pending_consumes"].append(item)
        cur = _state.get("tokens")
        if cur is not None:
            _state["tokens"] = max(0, int(cur) - int(amount))
    save()
    return item


def pending_consumes() -> list:
    with _lock:
        return list(_state["pending_consumes"])


def ack_consume(idempotency_key: str) -> None:
    with _lock:
        _state["pending_consumes"] = [
            p for p in _state["pending_consumes"] if p["idempotency_key"] != idempotency_key
        ]
    save()


# ---------- warning bookkeeping ----------
def get_notified(key: str):
    with _lock:
        return _state["notified"].get(key)


def set_notified(key: str, value) -> None:
    with _lock:
        _state["notified"][key] = value
    save()
