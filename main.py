"""AudioDeviceAgent entry point.

Boot order:
  1. Load persisted session (if any).
  2. Show the focusable Sign-in / Sign-up dialog when there's no session.
     This dialog is a normal window — deliberately NOT on the stealth path.
  3. Create the invisible AnswerOverlay + region selector, wire hotkeys,
     start the token-balance background sync, and run the Qt loop.
"""
import logging
import os
import queue
import sys
import tempfile
import random
import threading
import time

import keyboard

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

import answer
import api_client
import auth
import config
from capture import capture_region
import ocr
from overlay import AnswerOverlay
from selector import RegionSelector
import session
import winapi

log = logging.getLogger("agent")

selector = None
app = None
panel = None

task_q = queue.Queue()

CAPTURE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()),
                           "AudioDeviceAgent")
os.makedirs(CAPTURE_DIR, exist_ok=True)


def _setup_logging():
    if getattr(sys, "frozen", False):
        log_dir = os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()),
                               "AudioDeviceAgent")
        os.makedirs(log_dir, exist_ok=True)
        logging.basicConfig(
            filename=os.path.join(log_dir, "agent.log"),
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s")
    else:
        logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                            format="%(levelname)s %(message)s")


def dispatch(fn):
    task_q.put(fn)


def drain_queue():
    while True:
        try:
            fn = task_q.get_nowait()
        except queue.Empty:
            return
        try:
            fn()
        except Exception:
            log.exception("[hotkey] handler error")


# ---------- token sync ----------
_sync_lock = threading.Lock()
_backoff_s = 0
_last_sync_attempt = 0.0


def _refresh_balance_async():
    def _t():
        global _backoff_s, _last_sync_attempt
        if not _sync_lock.acquire(blocking=False):
            return  # a sync is already running
        try:
            # Exponential backoff after failures (timer still fires every tick).
            if _backoff_s and time.time() - _last_sync_attempt < _backoff_s:
                return
            _last_sync_attempt = time.time()
            _flush_pending_consumes()
            bal = api_client.get_balance()
            session.set_balance(bal)
            _backoff_s = 0
            dispatch(_refresh_idle_if_showing)
            dispatch(lambda: _maybe_warn(bal))
        except api_client.AuthExpired:
            dispatch(_session_ended)
        except api_client.ApiError as e:
            _backoff_s = min(config.TOKEN_REFRESH_MAX_BACKOFF_S,
                             (_backoff_s * 2 or 15) + random.uniform(0, 5))
            log.info("[tokens] balance refresh skipped (%s); retry in ~%ds", e, _backoff_s)
        finally:
            _sync_lock.release()
    threading.Thread(target=_t, daemon=True).start()


def _flush_pending_consumes():
    """Send debits queued while offline. Idempotency keys make retries safe."""
    for item in session.pending_consumes():
        try:
            bal = api_client.consume(item["idempotency_key"], item["amount"])
            session.ack_consume(item["idempotency_key"])
            session.set_balance(bal)
        except api_client.InsufficientCredits:
            # Server already knows the true balance; drop the stale debit.
            session.ack_consume(item["idempotency_key"])
        except api_client.AuthExpired:
            raise
        except api_client.ApiError as e:
            if e.status and 400 <= e.status < 500:
                session.ack_consume(item["idempotency_key"])  # unrecoverable, don't loop
            else:
                raise  # network/5xx: keep queued, retry later


def _maybe_warn(bal):
    """Show each low-balance / expiry warning once per crossing (Qt thread)."""
    warning = (bal or {}).get("warning") or {}
    level, msg = warning.get("level"), warning.get("message")
    if not msg or level in (None, "none"):
        session.set_notified("last", None)
        return
    remaining = int(bal.get("questions_remaining", 0))
    nxt = bal.get("next_expiry") or {}
    key = f"{level}:{remaining if level in ('low', 'critical', 'empty') else nxt.get('id')}"
    if level in ("low", "critical"):
        # Re-show only when the balance crosses a lower threshold.
        prev = session.get_notified("low_threshold")
        thresholds = bal.get("low_balance_thresholds") or [10, 3, 0]
        crossed = min([t for t in thresholds if remaining <= t], default=None)
        if prev is not None and crossed is not None and crossed >= prev:
            return
        session.set_notified("low_threshold", crossed)
    elif session.get_notified("last") == key:
        return
    session.set_notified("last", key)
    if level == "empty":
        panel.show_out_of_tokens()
        panel.setVisible(True)
        return
    # Never trample an answer the user is reading.
    if _panel_is_idle():
        panel.set_message(f"{msg}  Top up: {config.TOPUP_URL}", "warning")
        panel.setVisible(True)
    else:
        _deferred_warning[0] = f"{msg}  Top up: {config.TOPUP_URL}"


_deferred_warning = [None]


def _panel_is_idle():
    return bool(panel and panel._sections and len(panel._sections) == 1
                and panel._sections[0][0] == "html")


def _session_ended():
    panel.set_message("Your session ended. Restart the app to sign in again.", "error")
    panel.setVisible(True)


def _refresh_idle_if_showing():
    # Only re-render the idle cheat sheet — never trample an active answer.
    if panel and panel.isVisible() and panel._sections \
            and len(panel._sections) == 1 and panel._sections[0][0] == "html":
        panel.show_idle()


# ---------- actions (Qt thread) ----------
def act_toggle():
    panel.setVisible(not panel.isVisible())


def act_clear():
    panel.show_idle()
    if _deferred_warning[0]:
        msg, _deferred_warning[0] = _deferred_warning[0], None
        panel.set_message(msg, "warning")
    # Keep visible — the idle cheat sheet IS the "cleared" state, per spec.
    panel.setVisible(True)


def act_snip():
    # Fast, local pre-check: don't dim the screen if the user is out.
    tokens = session.get_tokens()
    if tokens is not None and tokens < config.TOKENS_PER_QUESTION:
        panel.show_out_of_tokens()
        panel.setVisible(True)
        return
    if selector is not None and not selector.isVisible():
        selector.start()


# ---------- hotkey callbacks (keyboard thread) ----------
def on_toggle():         dispatch(act_toggle)
def on_clear():          dispatch(act_clear)
def on_opacity_down():   dispatch(panel.opacity_down)
def on_opacity_up():     dispatch(panel.opacity_up)
def on_scroll_up():      dispatch(panel.scroll_up)
def on_scroll_dn():      dispatch(panel.scroll_down)
def on_snip():           dispatch(act_snip)
def on_nudge(dx, dy):    dispatch(lambda: panel.nudge(dx, dy))


def on_selector_finished(rect):
    if rect is None:
        log.info("[snip] cancelled")
        return
    pix = capture_region(rect)
    pix.save(os.path.join(CAPTURE_DIR, "last_capture.png"))
    panel.set_message("Reading question…")
    panel.setVisible(True)
    threading.Thread(target=_pipeline_worker, args=(pix,), daemon=True).start()


def _consume_and_reconcile(item):
    """Server-authoritative debit. Runs after the answer is already shown."""
    try:
        bal = api_client.consume(item["idempotency_key"], item["amount"])
        session.ack_consume(item["idempotency_key"])
        session.set_balance(bal)
        dispatch(lambda: _maybe_warn(bal))
    except api_client.InsufficientCredits:
        session.ack_consume(item["idempotency_key"])
        _refresh_balance_async()
    except api_client.AuthExpired:
        dispatch(_session_ended)
    except api_client.ApiError as e:
        # Offline / server error: stays queued and is flushed on next sync.
        log.warning("[tokens] consume deferred: %s", e)


def _pipeline_worker(pix):
    # OCR
    try:
        text = ocr.extract_text(pix)
    except Exception:
        log.exception("[ocr] extraction failed")
        dispatch(lambda: panel.set_message("Couldn't read that region. Try a larger box.", "error"))
        return
    if not text.strip():
        dispatch(lambda: panel.set_message(
            "No text found — try a wider or taller selection.", "warning"))
        return

    dispatch(lambda: panel.set_qa(text, "Thinking…", None))

    # Answer
    try:
        result = answer.answer_educational_question(text)
    except Exception:
        log.exception("[answer] call failed")
        dispatch(lambda: panel.set_message("Answer failed — see log.", "error"))
        return

    ok = not result.startswith("(answer")
    status = "Answer" if ok else "Retry later"
    dispatch(lambda: panel.set_qa(text, status, result))

    if ok:
        # Optimistic local debit first (instant), then reconcile with server.
        item = session.queue_consume(config.TOKENS_PER_QUESTION)
        threading.Thread(target=_consume_and_reconcile, args=(item,), daemon=True).start()


def main():
    global app, panel, selector

    _setup_logging()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # --- Onboarding: sign-in / sign-up (focusable, no stealth) ---
    session.load()
    if not auth.run_auth_flow():
        log.info("[auth] user cancelled sign-in; exiting")
        return

    # Preload OCR asynchronously
    threading.Thread(target=ocr.preload, daemon=True).start()

    # Build the stealth panel
    panel = AnswerOverlay()
    panel.show()
    if sys.platform == "win32":
        winapi.apply_stealth(panel, click_through=True)
        winapi.show_without_activation(panel)

    # Region selector
    selector = RegionSelector()
    selector.finished.connect(on_selector_finished)
    selector.status.connect(lambda msg: panel.set_message(msg, "warning"))

    # Qt-thread task pump
    pump = QTimer()
    pump.setInterval(10)
    pump.timeout.connect(drain_queue)
    pump.start()

    # Background token balance sync
    _refresh_balance_async()
    balance_timer = QTimer()
    balance_timer.setInterval(int(config.TOKEN_REFRESH_SECONDS * 1000))
    balance_timer.timeout.connect(_refresh_balance_async)
    balance_timer.start()

    # Hotkeys
    for combo, cb in [
        (config.HOTKEY_SNIP, on_snip),
        (config.HOTKEY_TOGGLE, on_toggle),
        (config.HOTKEY_CLEAR, on_clear),
        (config.HOTKEY_OPACITY_DN, on_opacity_down),
        (config.HOTKEY_OPACITY_UP, on_opacity_up),
        (config.HOTKEY_NUDGE_L, lambda: on_nudge(-40, 0)),
        (config.HOTKEY_NUDGE_R, lambda: on_nudge(+40, 0)),
        (config.HOTKEY_NUDGE_U, lambda: on_nudge(0, -40)),
        (config.HOTKEY_NUDGE_D, lambda: on_nudge(0, +40)),
        (config.HOTKEY_SCROLL_UP, on_scroll_up),
        (config.HOTKEY_SCROLL_DN, on_scroll_dn),
    ]:
        try:
            keyboard.add_hotkey(combo, cb, suppress=True)
        except Exception:
            log.exception("[hotkey] failed to register %s", combo)

    log.info("[agent] running. Shortcuts active.")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
