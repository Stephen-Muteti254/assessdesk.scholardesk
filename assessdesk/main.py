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
import threading

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
def _refresh_balance_async():
    def _t():
        try:
            n = api_client.get_balance()
            session.set_tokens(n)
            dispatch(_refresh_idle_if_showing)
        except api_client.ApiError as e:
            log.info("[tokens] balance refresh skipped: %s", e)
    threading.Thread(target=_t, daemon=True).start()


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


def _consume_and_reconcile():
    """Server-authoritative debit. Runs after we already returned the answer."""
    try:
        n = api_client.consume_tokens(config.TOKENS_PER_QUESTION)
        session.set_tokens(n)
    except api_client.ApiError as e:
        log.warning("[tokens] consume failed: %s", e)


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
        session.decrement_tokens(config.TOKENS_PER_QUESTION)
        threading.Thread(target=_consume_and_reconcile, daemon=True).start()


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
