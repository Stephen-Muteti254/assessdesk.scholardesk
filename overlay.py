"""The answer panel: topmost, click-through, capture-excluded, never-focus.

Renders four kinds of content in a single consistent style:
  - Idle    : the shortcut cheat sheet ("awaiting question")
  - Status  : short one-line state ("Reading question…", "Thinking…")
  - Question: the OCR'd prompt as plain text
  - Answer  : model output rendered as Markdown (code, tables, math)

The window itself is unchanged where it matters: still frameless, top-most,
non-activating, click-through, and excluded from screen captures via the
stealth pipeline in winapi.py.
"""
import ctypes.wintypes as wt
import html as _html
import logging
import re
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QTextDocument
from PySide6.QtWidgets import QApplication, QWidget

import config
import session
import winapi

# Optional deps — degrade gracefully if the environment doesn't have them.
try:
    import markdown as _md
    _MD = _md.Markdown(extensions=["fenced_code", "tables", "sane_lists",
                                   "nl2br", "codehilite"],
                       extension_configs={"codehilite": {"noclasses": True,
                                                         "pygments_style": "monokai"}})
except Exception:  # pragma: no cover
    _MD = None


PANEL_FONT = QFont("Segoe UI", 11)
PAD = 22
MAX_H_FRAC = 0.80

COL_BG        = QColor(config.OVERLAY_BG_COLOR)
COL_TEXT      = QColor(config.OVERLAY_TEXT_COLOR)
COL_MUTED     = QColor("#8b95a3")
COL_ACCENT    = QColor("#7fb3ff")
COL_ERROR     = QColor("#ff8a8a")
COL_WARNING   = QColor("#ffd166")
COL_STATUS    = QColor("#74c0fc")
COL_SUCCESS   = QColor("#7cd992")
COL_RULE      = QColor("#242c37")

TYPED = {
    "error":   ("Error",   COL_ERROR),
    "warning": ("Notice",  COL_WARNING),
    "success": ("Ready",   COL_SUCCESS),
}

# ---------- shared HTML styling ----------
BASE_CSS = """
    body {
        color: #e8eaed;
        font-family: 'Segoe UI', 'Inter', sans-serif;
        font-size: 13px;
        line-height: 1.5;
    }
    h1, h2, h3 { color: #ffffff; margin: 6px 0 4px 0; }
    h1 { font-size: 16px; }
    h2 { font-size: 14px; }
    h3 { font-size: 13px; }
    p  { margin: 4px 0; }
    ul, ol { margin: 4px 0 4px 18px; padding: 0; }
    li { margin: 2px 0; }
    strong { color: #ffffff; }
    em     { color: #cbd3de; }
    a  { color: #7fb3ff; }
    code {
        font-family: 'Consolas','JetBrains Mono','Courier New',monospace;
        background-color: #1a2029;
        color: #f8f8f2;
        border-radius: 3px;
        padding: 1px 5px;
        font-size: 12px;
    }
    pre {
        font-family: 'Consolas','JetBrains Mono','Courier New',monospace;
        background-color: #14181f;
        color: #f8f8f2;
        border: 1px solid #232a35;
        border-radius: 6px;
        padding: 10px 12px;
        font-size: 12px;
        line-height: 1.45;
    }
    pre code { background: transparent; padding: 0; border-radius: 0; }
    blockquote {
        border-left: 3px solid #2f6feb;
        margin: 6px 0; padding: 2px 10px;
        color: #cfd7e1; background-color: #141922;
    }
    table { border-collapse: collapse; margin: 6px 0; }
    th, td { border: 1px solid #2a323f; padding: 4px 8px; font-size: 12px; }
    th { background-color: #171d26; color: #ffffff; }
    .math-inline {
        font-family: 'Cambria Math','Consolas',serif;
        color: #ffe08a;
        background-color: #1a2029;
        border-radius: 3px;
        padding: 0 4px;
    }
    .math-block {
        display: block;
        font-family: 'Cambria Math','Consolas',serif;
        color: #ffe08a;
        background-color: #14181f;
        border: 1px solid #232a35;
        border-radius: 6px;
        padding: 10px 12px;
        margin: 6px 0;
        text-align: center;
    }
    .kbd {
        font-family: 'Consolas','JetBrains Mono',monospace;
        background: #1c222c;
        border: 1px solid #2a323f;
        border-bottom-width: 2px;
        border-radius: 4px;
        padding: 1px 6px;
        color: #e8eaed;
        font-size: 11px;
    }
    .row { margin: 3px 0; }
    .row .k { color: #cbd3de; }
    .row .v { color: #ffffff; }
    .header {
        color: #7fb3ff;
        font-size: 11px;
        letter-spacing: 1px;
        text-transform: uppercase;
    }
    .muted { color: #8b95a3; font-size: 12px; }
"""


# ---------- helpers ----------
def _kbd(s: str) -> str:
    return f'<span class="kbd">{_html.escape(s)}</span>'


def _protect_math(text: str):
    """Pull $$...$$ and $...$ out before Markdown, restore after."""
    slots = []

    def stash(m):
        body = m.group(1)
        cls = "math-block" if m.group(0).startswith("$$") else "math-inline"
        slots.append(f'<span class="{cls}">{_html.escape(body)}</span>')
        return f"@@MATH{len(slots)-1}@@"

    text = re.sub(r"\$\$(.+?)\$\$", stash, text, flags=re.S)
    text = re.sub(r"\$([^\$\n]+?)\$", stash, text)
    return text, slots


def _restore_math(html_str: str, slots) -> str:
    for i, val in enumerate(slots):
        html_str = html_str.replace(f"@@MATH{i}@@", val)
    return html_str


def render_markdown(text: str) -> str:
    """Answer text -> styled HTML. Falls back to plain-text on missing deps."""
    if not text:
        return ""
    if _MD is None:
        safe = _html.escape(text).replace("\n", "<br>")
        return f"<body>{safe}</body>"
    prepared, slots = _protect_math(text)
    _MD.reset()
    body = _MD.convert(prepared)
    body = _restore_math(body, slots)
    return f"<body>{body}</body>"


def idle_html(tokens) -> str:
    tok_line = ""
    if tokens is not None:
        tok_line = (
            f'<div class="row"><span class="k">Tokens remaining</span> · '
            f'<span class="v">{int(tokens)}</span></div>'
        )
    return f"""<body>
<div class="header">Awaiting question</div>
<p>Press {_kbd('Ctrl')}+{_kbd('Alt')}+{_kbd('Q')} to capture a question from your screen.</p>
<div class="header" style="margin-top:10px;">Controls</div>
<div class="row"><span class="k">Show / hide panel</span> · {_kbd('Ctrl')}+{_kbd('Shift')}+{_kbd('H')}</div>
<div class="row"><span class="k">Clear</span> · {_kbd('Ctrl')}+{_kbd('Shift')}+{_kbd('C')}</div>
<div class="row"><span class="k">Opacity −  /  +</span> · {_kbd('Ctrl')}+{_kbd('Shift')}+{_kbd('[')}  /  {_kbd('Ctrl')}+{_kbd('Shift')}+{_kbd(']')}</div>
<div class="row"><span class="k">Move panel</span> · {_kbd('Ctrl')}+{_kbd('Shift')}+{_kbd('←')} {_kbd('→')} {_kbd('↑')} {_kbd('↓')}</div>
<div class="row"><span class="k">Scroll answer</span> · {_kbd('Ctrl')}+{_kbd('Shift')}+{_kbd('PgUp')}  /  {_kbd('PgDn')}</div>
{tok_line}
</body>"""


def out_of_tokens_html() -> str:
    return f"""<body>
<div class="header" style="color:#ff8a8a;">Out of tokens</div>
<p>You've used your available answer tokens. Top up your balance from your
account dashboard to continue.</p>
<p class="muted">Your other controls still work — {_kbd('Ctrl')}+{_kbd('Shift')}+{_kbd('H')} to hide, {_kbd('Ctrl')}+{_kbd('Shift')}+{_kbd('C')} to clear.</p>
</body>"""


class AnswerOverlay(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.Window
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setWindowOpacity(config.OPACITY_LEVELS[config.DEFAULT_OPACITY_INDEX])
        self._opacity_index = config.DEFAULT_OPACITY_INDEX
        self.setFont(PANEL_FONT)

        self._sections = []  # list of (role, document_or_None)
        self._content_h = 0
        self._scroll = 0
        self._blank_render = False

        self.winId()  # force HWND creation for Win32 stealth calls

        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(config.OVERLAY_MAX_WIDTH, 220)
        self.move(
            screen.right() - self.width() - config.OVERLAY_MARGIN_PX,
            screen.bottom() - self.height() - config.OVERLAY_MARGIN_PX,
        )

        # Initial content = idle cheat sheet
        self.show_idle()

    # ---------- public API ----------
    def show_idle(self):
        """Empty / awaiting-question state. Also used after Clear."""
        tokens = session.get_tokens()
        self._set_sections([("html", idle_html(tokens))])

    def show_out_of_tokens(self):
        self._set_sections([("html", out_of_tokens_html())])

    def set_message(self, text: str, kind: str = "status"):
        """One-shot status / warning / error message."""
        if kind == "status":
            self._set_sections([("status", text)])
        else:
            tag, _ = TYPED.get(kind, ("Info", COL_TEXT))
            self._set_sections([(kind, f"{tag}: {text}")])

    def set_qa(self, question: str, status_line: str, answer: str = None):
        sections = [("status", status_line)]
        if question:
            sections.append(("question", question))
        if answer is not None:
            sections = [
                ("status", status_line),
                ("question", question),
                ("rule", None),
                ("answer", answer),
            ]
        self._set_sections(sections)

    def set_text(self, text: str):
        """Legacy convenience."""
        if not text:
            self.show_idle()
            return
        self._set_sections([("answer", text)])

    def nudge(self, dx, dy):
        self.move(self.x() + dx, self.y() + dy)

    def opacity_down(self):
        self._opacity_index = min(
            self._opacity_index + 1, len(config.OPACITY_LEVELS) - 1
        )
        self.setWindowOpacity(config.OPACITY_LEVELS[self._opacity_index])

    def opacity_up(self):
        self._opacity_index = max(self._opacity_index - 1, 0)
        self.setWindowOpacity(config.OPACITY_LEVELS[self._opacity_index])

    # ---------- native event & stealth helpers ----------
    def nativeEvent(self, eventType, message):
        if eventType == b"windows_generic_MSG":
            msg = wt.MSG.from_address(int(message))
            if msg.message in (winapi.WM_PRINT, winapi.WM_PRINTCLIENT):
                self._blank_render = True
                self.update()
        return super().nativeEvent(eventType, message)

    # ---------- internal helpers & layout ----------
    def _create_doc_for_role(self, role: str, text: str) -> QTextDocument:
        doc = QTextDocument(self)
        doc.setTextWidth(config.OVERLAY_MAX_WIDTH - 2 * PAD)
        doc.setDefaultStyleSheet(BASE_CSS)

        if role == "answer":
            doc.setHtml(render_markdown(text))
            return doc
        if role == "html":
            doc.setHtml(text)
            return doc

        if role == "status":
            color = COL_STATUS.name()
            safe = _html.escape(text).replace("\n", "<br>")
            doc.setHtml(
                f'<body><div class="header" style="color:{color};">{safe}</div></body>'
            )
            return doc

        if role == "question":
            safe = _html.escape(text).replace("\n", "<br>")
            doc.setHtml(
                f'<body>'
                f'<div class="header" style="color:{COL_MUTED.name()};">Question</div>'
                f'<div style="color:#e8eaed;">{safe}</div>'
                f'</body>'
            )
            return doc

        if role in TYPED:
            color = TYPED[role][1].name()
            safe = _html.escape(text).replace("\n", "<br>")
            doc.setHtml(f'<body style="color:{color};">{safe}</body>')
            return doc

        safe = _html.escape(text).replace("\n", "<br>")
        doc.setHtml(f'<body>{safe}</body>')
        return doc

    def _set_sections(self, raw_sections):
        self._sections = []
        h = 0
        fm = self.fontMetrics()
        for role, text in raw_sections:
            if role == "rule":
                self._sections.append(("rule", None))
                h += fm.lineSpacing() + 6
            else:
                doc = self._create_doc_for_role(role, text)
                self._sections.append((role, doc))
                h += int(doc.size().height()) + 8

        self._content_h = h + 2 * PAD
        self._scroll = 0

        screen = QApplication.primaryScreen().availableGeometry()
        max_h = int(screen.height() * MAX_H_FRAC)
        new_h = max(min(self._content_h, max_h), 120)
        old_h = self.height()

        self.resize(config.OVERLAY_MAX_WIDTH, new_h)
        self.move(self.x(), max(0, self.y() + (old_h - new_h)))
        self.update()

    # ---------- scrolling ----------
    def scroll_up(self):
        self._scroll = max(0, self._scroll - 80)
        self.update()

    def scroll_down(self):
        mx = max(0, self._content_h - self.height())
        self._scroll = min(mx, self._scroll + 80)
        self.update()

    # ---------- painting ----------
    def paintEvent(self, event):
        p = QPainter(self)

        # Capture mitigation: blank if the compositor asked for a WM_PRINT copy.
        if self._blank_render:
            p.fillRect(self.rect(), QColor(255, 255, 255, 255))
            self._blank_render = False
            return

        p.setPen(Qt.NoPen)
        p.setBrush(COL_BG)
        p.drawRect(self.rect())
        p.setFont(PANEL_FONT)

        p.save()
        p.setClipRect(self.rect().adjusted(2, 2, -2, -2))

        y = PAD - self._scroll
        fm = p.fontMetrics()

        for role, doc in self._sections:
            if role == "rule":
                p.setPen(COL_RULE)
                line_y = y + fm.lineSpacing() // 2
                p.drawLine(PAD, line_y, self.width() - PAD, line_y)
                y += fm.lineSpacing() + 6
                continue

            doc_h = int(doc.size().height())
            if (y + doc_h) >= 0 and y <= self.height():
                p.save()
                p.translate(PAD, y)
                doc.drawContents(p)
                p.restore()
            y += doc_h + 8

        p.restore()

        # thin scrollbar indicator
        max_scroll = max(0, self._content_h - self.height())
        if max_scroll > 0:
            track_x = self.width() - 6
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 40))
            p.drawRoundedRect(track_x, 4, 3, self.height() - 8, 1, 1)
            track_h = self.height() - 8
            thumb_h = max(20, int(track_h * self.height() / self._content_h))
            thumb_y = 4 + int((track_h - thumb_h) * self._scroll / max_scroll)
            p.setBrush(QColor(255, 255, 255, 140))
            p.drawRoundedRect(track_x, thumb_y, 3, thumb_h, 1, 1)

    # ---------- stealth ----------
    def showEvent(self, event):
        if sys.platform == "win32":
            winapi.show_without_activation(self)
            QTimer.singleShot(100, self._late_stealth)
        super().showEvent(event)

    def _late_stealth(self):
        if not self.isVisible():
            return
        winapi.apply_stealth(self)
        logging.info("[winapi] late apply; hidden-from-capture: %s",
                     winapi.is_hidden_from_capture(self))
