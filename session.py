"""Local session persistence: auth token + cached token balance.

Kept intentionally small — the server is the source of truth. This just
avoids a network round-trip on every hotkey and survives restarts.
"""
import json
import logging
import os
import threading
from typing import Optional

import config

log = logging.getLogger("session")
_lock = threading.Lock()

_state = {
    "auth_token": None,   # bearer token from /auth/login
    "user": None,         # {"id","full_name","email"}
    "tokens": None,       # int, cached answer-tokens remaining
}


def load() -> dict:
    """Load session from disk into memory. Silent on missing/corrupt files."""
    with _lock:
        try:
            with open(config.SESSION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            _state.update({k: data.get(k) for k in _state.keys()})
        except (FileNotFoundError, ValueError, OSError):
            pass
        return dict(_state)


def save() -> None:
    with _lock:
        try:
            os.makedirs(config.SESSION_DIR, exist_ok=True)
            with open(config.SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump(_state, f)
        except OSError:
            log.exception("[session] failed to persist session file")


def set_auth(auth_token: str, user: dict, tokens: Optional[int]) -> None:
    with _lock:
        _state["auth_token"] = auth_token
        _state["user"] = user
        if tokens is not None:
            _state["tokens"] = int(tokens)
    save()


def clear() -> None:
    with _lock:
        for k in _state:
            _state[k] = None
    try:
        os.remove(config.SESSION_FILE)
    except OSError:
        pass


def get() -> dict:
    with _lock:
        return dict(_state)


def is_authenticated() -> bool:
    with _lock:
        return bool(_state.get("auth_token"))


def get_tokens() -> Optional[int]:
    with _lock:
        return _state.get("tokens")


def set_tokens(n: Optional[int]) -> None:
    with _lock:
        _state["tokens"] = None if n is None else int(n)
    save()


def decrement_tokens(n: int = 1) -> Optional[int]:
    """Optimistic local decrement — server call reconciles the true value."""
    with _lock:
        cur = _state.get("tokens")
        if cur is None:
            return None
        _state["tokens"] = max(0, int(cur) - n)
        val = _state["tokens"]
    save()
    return val
