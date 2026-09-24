"""HTTP client for the AssessDesk backend (ScholarDesk API).

Auth: short-lived access token in `Authorization: Bearer`, renewed
transparently with the device refresh token (rotated on every use).
See API.md for the contract.
"""
import logging
import platform
import threading
from typing import Optional

import requests

import config
import session

log = logging.getLogger("api")

_http = requests.Session()  # connection pooling / keep-alive
_refresh_lock = threading.Lock()


class ApiError(Exception):
    """Server/network error with a user-presentable message."""

    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


class AuthExpired(ApiError):
    """Session can't be renewed — the user must sign in again."""


class InsufficientCredits(ApiError):
    def __init__(self, message: str, remaining: int = 0):
        super().__init__(message, 402, "INSUFFICIENT_CREDITS")
        self.remaining = remaining


def _url(path: str) -> str:
    return f"{config.API_BASE_URL}{path}"


def _extract_error(r):
    try:
        data = r.json()
    except ValueError:
        return f"Server error ({r.status_code}).", "", {}
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return err.get("message") or "Request failed.", err.get("code", ""), err.get("details") or {}
    if isinstance(err, str):
        return err, "", {}
    return (data.get("message") if isinstance(data, dict) else None) or "Request failed.", "", {}


def _send(method, path, *, token=None, json_body=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        return _http.request(method, _url(path), headers=headers, json=json_body,
                             timeout=config.API_TIMEOUT_S)
    except requests.RequestException as e:
        raise ApiError("Can't reach the server. Check your connection.", 0, "NETWORK") from e


def _parse(r):
    if r.status_code >= 400:
        msg, code, details = _extract_error(r)
        if r.status_code == 402:
            raise InsufficientCredits(msg, int(details.get("questions_remaining", 0)))
        raise ApiError(msg, r.status_code, code)
    try:
        return r.json()
    except ValueError:
        return {}


def _refresh_access() -> str:
    """Renew the access token once, even if several threads need it at the same moment."""
    with _refresh_lock:
        tok = session.get_access_token()
        if tok:
            return tok
        refresh = session.get_refresh_token()
        if not refresh:
            raise AuthExpired("Please sign in again.", 401)
        r = _send("POST", "/auth/refresh", json_body={"refresh_token": refresh})
        if r.status_code in (401, 403):
            session.clear()
            raise AuthExpired("Your session ended. Please sign in again.", r.status_code)
        data = _parse(r)
        session.set_device_session(data)
        return data["access_token"]


def _authed(method, path, json_body=None):
    token = session.get_access_token() or _refresh_access()
    r = _send(method, path, token=token, json_body=json_body)
    if r.status_code == 401:
        session.set_access(None, 0)
        token = _refresh_access()
        r = _send(method, path, token=token, json_body=json_body)
        if r.status_code == 401:
            session.clear()
            raise AuthExpired("Your session ended. Please sign in again.", 401)
    return _parse(r)


# ---------- Auth (sign-in only; accounts are created on the website) ----------
def login(email: str, password: str) -> str:
    """POST /auth/login -> otp_session_id (a code is emailed)."""
    data = _parse(_send("POST", "/auth/login", json_body={"email": email, "password": password}))
    return data["otp_session_id"]


def resend_otp(email: str) -> str:
    data = _parse(_send("POST", "/auth/resend-otp", json_body={"email": email}))
    return data["otp_session_id"]


def verify_otp(otp_session_id: str, otp: str) -> dict:
    """POST /auth/verify-otp -> stores the device session, returns payload."""
    data = _parse(_send("POST", "/auth/verify-otp", json_body={
        "otp_session_id": otp_session_id,
        "otp": otp,
        "client_device_id": session.client_device_id(),
        "device_name": platform.node()[:120],
        "client_version": config.APP_VERSION,
    }))
    session.set_device_session(data)
    return data


def logout() -> None:
    try:
        _authed("POST", "/auth/logout")
    except ApiError:
        pass
    session.clear()


# ---------- Balance ----------
def get_balance() -> dict:
    """GET /me/balance -> full balance payload (questions, expiry, warning)."""
    return _authed("GET", "/me/balance")


def consume(idempotency_key: str, amount: int = 1, meta: Optional[dict] = None) -> dict:
    """POST /me/consume — idempotent server-authoritative debit."""
    body = {"amount": amount, "idempotency_key": idempotency_key,
            "meta": meta or {"reason": "answer", "client_version": config.APP_VERSION}}
    return _authed("POST", "/me/consume", body)
