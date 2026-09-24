"""Sign-in window (email + password, then emailed 6-digit code).

A NORMAL focusable dialog — deliberately outside the stealth pipeline.
Shown only when there's no device session. Accounts are created and
topped up on https://assessdesk.scholardesk.pro, so there is no sign-up
form here.

The native Windows title bar can't be themed from Qt, so the window is
frameless with its own dark header (drag to move, Esc or x to close).
"""
import logging
import re
import threading
import webbrowser

from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtGui import QFont, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QStackedWidget, QVBoxLayout, QWidget,
)
from PySide6.QtCore import QRegularExpression

import api_client
import config
import session

log = logging.getLogger("auth")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

STYLE = """
QDialog { background-color: #0f1115; }
QWidget#Root { background-color: #0f1115; border: 1px solid #1e2430; border-radius: 10px; }
QWidget#TitleBar { background-color: #0f1115; border-top-left-radius: 10px; border-top-right-radius: 10px; }
QLabel#TitleText { font-size: 12px; color: #9aa4b2; font-weight: 600; }
QPushButton#Close {
    background: transparent; border: none; color: #9aa4b2;
    font-size: 16px; padding: 0 10px; min-width: 32px; min-height: 28px;
}
QPushButton#Close:hover { background-color: #c42b1c; color: white; border-radius: 6px; }
QWidget#Card { background-color: #0f1115; color: #e8eaed; }
QLabel#Title { font-size: 20px; font-weight: 600; color: #ffffff; }
QLabel#Subtitle { font-size: 12px; color: #9aa4b2; }
QLabel { font-size: 12px; color: #c4ccd6; }
QLineEdit {
    background-color: #171b22; border: 1px solid #262c36; border-radius: 6px;
    padding: 9px 11px; font-size: 13px; color: #ffffff;
    selection-background-color: #2b6cb0;
}
QLineEdit:focus { border: 1px solid #4da3ff; }
QLineEdit#Otp { font-size: 22px; letter-spacing: 8px; padding: 10px; }
QPushButton#Primary {
    background-color: #2f6feb; color: white; border: none; border-radius: 6px;
    padding: 10px 14px; font-size: 13px; font-weight: 600;
}
QPushButton#Primary:hover  { background-color: #3a7cf5; }
QPushButton#Primary:disabled { background-color: #2a3140; color: #6b7280; }
QPushButton#Link {
    background: transparent; border: none; color: #7fb3ff;
    font-size: 12px; text-align: left; padding: 2px 0;
}
QPushButton#Link:hover { color: #a7cbff; }
QPushButton#Link:disabled { color: #4b5563; }
QLabel#Error   { color: #ff8a8a; font-size: 12px; }
QLabel#Success { color: #7cd992; font-size: 12px; }
QFrame#Divider { background-color: #1e2430; max-height: 1px; }
"""


class _Worker(QObject):
    """Runs a blocking API call off the UI thread and emits the result."""
    done = Signal(object, str)

    def run(self, fn):
        def _target():
            try:
                self.done.emit(fn(), "")
            except Exception as e:  # includes ApiError
                self.done.emit(None, str(e) or "Something went wrong.")
        threading.Thread(target=_target, daemon=True).start()


class TitleBar(QWidget):
    """Dark, draggable replacement for the native title bar."""
    close_clicked = Signal()

    def __init__(self, title: str):
        super().__init__()
        self.setObjectName("TitleBar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(36)
        self._drag = None
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 0, 6, 0)
        t = QLabel(title)
        t.setObjectName("TitleText")
        row.addWidget(t)
        row.addStretch(1)
        x = QPushButton("\u2715")
        x.setObjectName("Close")
        x.setCursor(Qt.PointingHandCursor)
        x.setToolTip("Close")
        x.clicked.connect(self.close_clicked.emit)
        row.addWidget(x)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.window().move(e.globalPosition().toPoint() - self._drag)
            e.accept()

    def mouseReleaseEvent(self, e):
        self._drag = None


class _Card(QWidget):
    def __init__(self, title: str, subtitle: str):
        super().__init__()
        self.setObjectName("Card")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(28, 12, 28, 26)
        self.root.setSpacing(12)
        t = QLabel(title); t.setObjectName("Title")
        self.subtitle = QLabel(subtitle); self.subtitle.setObjectName("Subtitle")
        self.subtitle.setWordWrap(True)
        self.root.addWidget(t)
        self.root.addWidget(self.subtitle)
        self.root.addSpacing(6)
        self.err = QLabel(""); self.err.setObjectName("Error")
        self.err.setWordWrap(True); self.err.hide()
        self.info = QLabel(""); self.info.setObjectName("Success")
        self.info.setWordWrap(True); self.info.hide()
        self._worker = _Worker()

    def show_error(self, msg):
        self.info.hide(); self.err.setText(msg); self.err.show()

    def show_info(self, msg):
        self.err.hide(); self.info.setText(msg); self.info.show()

    def clear_msgs(self):
        self.err.hide(); self.info.hide()

    def field(self, label, placeholder="", password=False):
        self.root.addWidget(QLabel(label))
        e = QLineEdit(); e.setPlaceholderText(placeholder)
        if password:
            e.setEchoMode(QLineEdit.Password)
        self.root.addWidget(e)
        return e

    def primary(self, text, slot):
        b = QPushButton(text); b.setObjectName("Primary")
        b.setCursor(Qt.PointingHandCursor); b.clicked.connect(slot)
        return b

    def link(self, text, slot):
        b = QPushButton(text); b.setObjectName("Link")
        b.setCursor(Qt.PointingHandCursor); b.clicked.connect(slot)
        return b

    def divider(self):
        d = QFrame(); d.setObjectName("Divider"); d.setFrameShape(QFrame.HLine)
        return d


class LoginForm(_Card):
    otp_sent = Signal(str, str)  # email, otp_session_id

    def __init__(self):
        super().__init__("Sign in", "Enter your AssessDesk account details. "
                                    "We'll email you a one-time code to confirm it's you.")
        self.email = self.field("Email", "you@example.com")
        self.password = self.field("Password", "Your password", password=True)
        self.email.returnPressed.connect(self._submit)
        self.password.returnPressed.connect(self._submit)
        self.root.addWidget(self.err)
        self.root.addSpacing(4)
        self.submit = self.primary("Continue", self._submit)
        self.root.addWidget(self.submit)
        self.root.addSpacing(6); self.root.addWidget(self.divider()); self.root.addSpacing(2)
        self.root.addWidget(self.link("No account or need more questions?  Visit assessdesk.scholardesk.pro",
                                      lambda: webbrowser.open(config.TOPUP_URL)))
        self.root.addStretch(1)
        self._worker.done.connect(self._on_result)

    def _submit(self):
        self.clear_msgs()
        email = self.email.text().strip()
        if not _EMAIL_RE.match(email):
            return self.show_error("Please enter a valid email address.")
        if not self.password.text():
            return self.show_error("Password is required.")
        self._email = email
        self.submit.setDisabled(True); self.submit.setText("Please wait…")
        pw = self.password.text()
        self._worker.run(lambda: api_client.login(email, pw))

    def _on_result(self, otp_session_id, err):
        self.submit.setDisabled(False); self.submit.setText("Continue")
        if err:
            return self.show_error(err)
        self.password.clear()
        self.otp_sent.emit(self._email, otp_session_id)


class OtpForm(_Card):
    back = Signal()
    success = Signal(dict)

    def __init__(self):
        super().__init__("Check your email", "")
        self.code = self.field("6-digit code", "••••••")
        self.code.setObjectName("Otp")
        self.code.setMaxLength(6)
        self.code.setAlignment(Qt.AlignCenter)
        self.code.setValidator(QRegularExpressionValidator(QRegularExpression(r"\d{0,6}")))
        self.code.textChanged.connect(lambda t: len(t) == 6 and self._submit())
        self.root.addWidget(self.err)
        self.root.addWidget(self.info)
        self.root.addSpacing(4)
        self.submit = self.primary("Verify and sign in", self._submit)
        self.root.addWidget(self.submit)
        self.root.addSpacing(6); self.root.addWidget(self.divider()); self.root.addSpacing(2)
        self.resend = self.link("Didn't get it?  Send a new code", self._resend)
        self.root.addWidget(self.resend)
        self.root.addWidget(self.link("Use a different account", self.back.emit))
        self.root.addStretch(1)
        self._busy = False
        self._worker.done.connect(self._on_verify)
        self._resend_worker = _Worker()
        self._resend_worker.done.connect(self._on_resend)

    def start(self, email, otp_session_id):
        self._email, self._otp_session_id = email, otp_session_id
        self.subtitle.setText(f"We sent a 6-digit code to {email}. It expires in a few minutes.")
        self.code.clear(); self.clear_msgs(); self.code.setFocus()

    def _submit(self):
        if self._busy:
            return
        code = self.code.text().strip()
        if len(code) != 6:
            return self.show_error("Enter the 6-digit code from your email.")
        self.clear_msgs()
        self._busy = True
        self.submit.setDisabled(True); self.submit.setText("Verifying…")
        sid = self._otp_session_id
        self._worker.run(lambda: api_client.verify_otp(sid, code))

    def _on_verify(self, payload, err):
        self._busy = False
        self.submit.setDisabled(False); self.submit.setText("Verify and sign in")
        if err:
            self.code.clear()
            return self.show_error(err)
        self.success.emit(payload)

    def _resend(self):
        self.resend.setDisabled(True)
        email = self._email
        self._resend_worker.run(lambda: api_client.resend_otp(email))

    def _on_resend(self, sid, err):
        self.resend.setDisabled(False)
        if err:
            return self.show_error(err)
        self._otp_session_id = sid
        self.show_info("A new code is on its way.")


class AuthDialog(QDialog):
    """Modal onboarding dialog. accept() once a device session is stored."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ExamAssist : Sign in")
        self.setModal(True)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(420, 540)
        self.setStyleSheet(STYLE)
        self.setFont(QFont("Segoe UI", 10))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        root = QWidget(); root.setObjectName("Root")
        root.setAttribute(Qt.WA_StyledBackground, True)
        outer.addWidget(root)
        col = QVBoxLayout(root)
        col.setContentsMargins(1, 1, 1, 1)
        col.setSpacing(0)

        bar = TitleBar("ExamAssist — Sign in")
        bar.close_clicked.connect(self.reject)
        col.addWidget(bar)

        self.stack = QStackedWidget()
        self.login = LoginForm()
        self.otp = OtpForm()
        self.stack.addWidget(self.login)
        self.stack.addWidget(self.otp)
        col.addWidget(self.stack)

        self.login.otp_sent.connect(self._to_otp)
        self.otp.back.connect(lambda: self.stack.setCurrentWidget(self.login))
        self.otp.success.connect(lambda _p: self.accept())

    def _to_otp(self, email, sid):
        self.otp.start(email, sid)
        self.stack.setCurrentWidget(self.otp)


def run_auth_flow(parent=None) -> bool:
    """Show the sign-in dialog if there's no device session. True on success."""
    if session.is_authenticated():
        return True
    dlg = AuthDialog()
    return dlg.exec() == QDialog.Accepted
