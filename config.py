"""Central configuration — hotkeys, opacity, model settings, identity."""
import os
import tempfile

# --- Product identity (used by build.bat for exe metadata) ---
APP_NAME        = "AudioDeviceAgent"
APP_COMPANY     = "Microsoft Corporation"
APP_PRODUCT     = "Windows Audio Device Bridge"
APP_DESCRIPTION = "System audio bridge service"
APP_VERSION     = "1.0.4.2"

# --- Hotkeys ---
HOTKEY_SNIP       = "ctrl+alt+q"
HOTKEY_TOGGLE     = "ctrl+shift+h"
HOTKEY_CLEAR      = "ctrl+shift+c"
HOTKEY_OPACITY_DN = "ctrl+shift+["
HOTKEY_OPACITY_UP  = "ctrl+shift+]"
HOTKEY_NUDGE_L     = "ctrl+shift+left"
HOTKEY_NUDGE_R     = "ctrl+shift+right"
HOTKEY_NUDGE_U     = "ctrl+shift+up"
HOTKEY_NUDGE_D     = "ctrl+shift+down"
HOTKEY_SCROLL_UP   = "ctrl+shift+page up"
HOTKEY_SCROLL_DN   = "ctrl+shift+page down"

# --- Opacity ---
OPACITY_LEVELS = [1.0, 0.8, 0.6, 0.35, 0.15]
DEFAULT_OPACITY_INDEX = 0

# --- Overlay appearance ---
OVERLAY_MARGIN_PX = 40
OVERLAY_MAX_WIDTH  = 520
OVERLAY_CORNER_RADIUS = 12
OVERLAY_BG_COLOR     = "#101418"
OVERLAY_TEXT_COLOR   = "#e8eaed"

# --- Answer engine ---
GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_MODEL_FALLBACKS = ["gemini-3.6-flash", "gemini-3.6-flash"]
GEMINI_TEMPERATURE = 0.3
GEMINI_MAX_RETRIES = 3
GEMINI_RETRY_DELAY_S = 4.0
ANSWER_MODE = "exam"     # "exam" | "tutor"

# --- OCR ---
OCR_UPSCALE = 2

# --- Backend / accounts ---
# Point this at your server. Override with EXAMASSIST_API_URL env var.
API_BASE_URL = os.environ.get(
    "EXAMASSIST_API_URL", "https://api.scholardesk.pro/api/v1/assessdesk"
).rstrip("/")
API_TIMEOUT_S = 8.0

# Where the local session token + cached balance live.
SESSION_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", tempfile.gettempdir()), "AudioDeviceAgent"
)
SESSION_FILE = os.path.join(SESSION_DIR, "session.json")

# Cost model — kept client-side for instant pre-checks. The server is the
# authority; local balance is a cache that gets reconciled after every answer.
TOKENS_PER_QUESTION = 1
TOKEN_REFRESH_SECONDS = 600  # background balance sync cadence
TOKEN_REFRESH_MAX_BACKOFF_S = 300  # cap for retry backoff after network errors

# Where users buy more questions (opened from warnings / out-of-tokens panel).
TOPUP_URL = "https://assessdesk.scholardesk.pro/pricing"

# Refresh token lives in Windows Credential Manager under this service name.
KEYRING_SERVICE = "AudioDeviceAgent"
