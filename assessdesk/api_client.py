"""Thin HTTP client for the ExamAssist backend.

All endpoints return JSON. Auth is a Bearer token in the Authorization
header. See API.md at the project root for the full contract.
"""
import logging
from typing import Optional, Tuple

import requests

import config
import session

log = logging.getLogger("api")


class ApiError(Exception):
    """Raised when the server returns an error we can present to the user."""


def _url(path: str) -> str:
    return f"{config.API_BASE_URL}{path}"


def _headers(auth: bool = False) -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if auth:
        tok = session.get().get("auth_token")
        if tok:
            h["Authorization"] = f"Bearer {tok}"
    return h


def _request(method: str, path: str, *, auth: bool = False, json_body=None):
    try:
        r = requests.request(
            method, _url(path),
            headers=_headers(auth=auth),
            json=json_body,
            timeout=config.API_TIMEOUT_S,
        )
    except requests.RequestException as e:
        raise ApiError(f"Network error: {e}") from e

    if r.status_code >= 400:
        msg = _extract_error(r)
        raise ApiError(msg)
    try:
        return r.json()
    except ValueError:
        return {}


def _extract_error(r) -> str:
    try:
        data = r.json()
        if isinstance(data, dict):
            return str(data.get("error") or data.get("message") or r.text)
    except ValueError:
        pass
    return f"HTTP {r.status_code}: {r.text[:200]}"


# ---------- Auth ----------
def register(full_name: str, email: str, password: str) -> dict:
    """POST /auth/register -> {auth_token, user, tokens}"""
    return _request("POST", "/auth/register", json_body={
        "full_name": full_name, "email": email, "password": password,
    })


def login(email: str, password: str) -> dict:
    """POST /auth/login -> {auth_token, user, tokens}"""
    return _request("POST", "/auth/login", json_body={
        "email": email, "password": password,
    })


# ---------- Tokens ----------
def get_balance() -> int:
    """GET /tokens/balance -> {tokens: int}"""
    data = _request("GET", "/tokens/balance", auth=True)
    return int(data.get("tokens", 0))


def consume_tokens(amount: int = 1, meta: Optional[dict] = None) -> int:
    """POST /tokens/consume -> {tokens: int} — server-authoritative debit."""
    body = {"amount": amount}
    if meta:
        body["meta"] = meta
    data = _request("POST", "/tokens/consume", auth=True, json_body=body)
    return int(data.get("tokens", 0))
