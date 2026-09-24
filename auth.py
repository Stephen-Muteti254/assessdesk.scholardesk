"""Sign-in and sign-up windows.

These are NORMAL focusable dialogs — deliberately outside the stealth
pipeline. They're only shown at onboarding, before the invisible overlay
is armed. Once the user is authenticated they're never displayed again
(unless the session is cleared or rejected by the server).
"""
import logging
import re
import threading

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QStackedWidget, QWidget, QFrame, QSizePolicy,
)

import api_client
import session

log = logging.getLogger("auth")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

STYLE = """
QDialog, QWidget#Card {
    background-color: #0f1115;
    color: #e8eaed;
}
QLabel#Title {
    font-size: 20px;
    font-weight: 600;
    color: #ffffff;
}
QLabel#Subtitle {
    font-size: 12px;
    color: #9aa4b2;
}
QLabel {
    font-size: 12px;
    color: #c4ccd6;
}
QLineEdit {
    background-color: #171b22;
    border: 1px solid #262c36;
    border-radius: 6px;
    padding: 9px 11px;
    font-size: 13px;
    color: #ffffff;
    selection-background-color: #2b6cb0;
}
QLineEdit:focus { border: 1px solid #4da3ff; }
QPushButton#Primary {
    background-color: #2f6feb;
    color: white;
    border: none;
    border-radius: 6px;
    padding: 10px 14px;
    font-size: 13px;
    font-weight: 600;
}
QPushButton#Primary:hover  { background-color: #3a7cf5; }
QPushButton#Primary:disabled { background-color: #2a3140; color: #6b7280; }
QPushButton#Link {
    background: transparent;
    border: none;
    color: #7fb3ff;
    font-size: 12px;
    text-align: left;
    padding: 2px 0;
}
QPushButton#Link:hover { color: #a7cbff; }
QLabel#Error   { color: #ff8a8a; font-size: 12px; }
QLabel#Success { color: #7cd992; font-size: 12px; }
QFrame#Divider { background-color: #1e2430; max-height: 1px; }
"""


class _Worker(QObject):
    """Runs a blocking API call off the UI thread and emits the result."""
    done = Signal(object, str)  # (payload_or_None, error_or_None)

    def run(self, fn):
        def _target():
            try:
                payload = fn()
                self.done.emit(payload, None)
            except Exception as e:  # includes ApiError
                self.done.emit(None, str(e))
        threading.Thread(target=_target, daemon=True).start()


class _FormBase(QWidget):
    switch = Signal()          # navigate to the other form
    success = Signal(dict)     # emits the auth payload

    def __init__(self, title: str, subtitle: str, submit_label: str,
                 alt_label: str):
        super().__init__()
        self.setObjectName("Card")
        self._worker = _Worker()
        self._worker.done.connect(self._on_result)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 26, 28, 26)
        root.setSpacing(12)

        t = QLabel(title); t.setObjectName("Title")
        s = QLabel(subtitle); s.setObjectName("Subtitle")
        s.setWordWrap(True)
        root.addWidget(t)
        root.addWidget(s)
        root.addSpacing(6)

        self.fields = {}
        self.build_fields(root)

        self.err = QLabel(""); self.err.setObjectName("Error")
        self.err.setWordWrap(True); self.err.hide()
        root.addWidget(self.err)

        root.addSpacing(4)
        self.submit = QPushButton(submit_label)
        self.submit.setObjectName("Primary")
        self.submit.setCursor(Qt.PointingHandCursor)
        self.submit.clicked.connect(self._submit)
        root.addWidget(self.submit)

        div = QFrame(); div.setObjectName("Divider"); div.setFrameShape(QFrame.HLine)
        root.addSpacing(6); root.addWidget(div); root.addSpacing(2)

        alt = QPushButton(alt_label); alt.setObjectName("Link")
        alt.setCursor(Qt.PointingHandCursor)
        alt.clicked.connect(self.switch.emit)
        root.addWidget(alt)

        root.addStretch(1)

    # ---- to override ----
    def build_fields(self, layout):
        raise NotImplementedError

    def call_api(self) -> dict:
        raise NotImplementedError

    # ---- helpers ----
    def _add_field(self, layout, key, label, *, placeholder="", password=False):
        lbl = QLabel(label)
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        if password:
            edit.setEchoMode(QLineEdit.Password)
        edit.returnPressed.connect(self._submit)
        layout.addWidget(lbl)
        layout.addWidget(edit)
        self.fields[key] = edit
        return edit

    def _show_error(self, msg: str):
        self.err.setText(msg); self.err.show()

    def _clear_error(self):
        self.err.hide(); self.err.setText("")

    def _set_busy(self, busy: bool):
        self.submit.setDisabled(busy)
        self.submit.setText("Please wait…" if busy else self._label)

    def _submit(self):
        self._clear_error()
        try:
            self._validate()
        except ValueError as ve:
            self._show_error(str(ve)); return
        self._label = self.submit.text()
        self._set_busy(True)
        self._worker.run(self.call_api)

    def _validate(self):
        pass

    def _on_result(self, payload, err):
        self._set_busy(False)
        if err:
            self._show_error(err); return
        self.success.emit(payload)


class LoginForm(_FormBase):
    def __init__(self):
        super().__init__(
            title="Sign in",
            subtitle="Welcome back. Enter your account details to continue.",
            submit_label="Sign in",
            alt_label="New here?  Create an account",
        )

    def build_fields(self, layout):
        self._add_field(layout, "email", "Email",
                        placeholder="you@example.com")
        self._add_field(layout, "password", "Password",
                        placeholder="Your password", password=True)

    def _validate(self):
        if not _EMAIL_RE.match(self.fields["email"].text().strip()):
            raise ValueError("Please enter a valid email address.")
        if not self.fields["password"].text():
            raise ValueError("Password is required.")

    def call_api(self):
        return api_client.login(
            self.fields["email"].text().strip(),
            self.fields["password"].text(),
        )


class RegisterForm(_FormBase):
    def __init__(self):
        super().__init__(
            title="Create your account",
            subtitle="Get started in seconds. You'll receive a starter "
                     "balance to try out the assistant.",
            submit_label="Create account",
            alt_label="Already have an account?  Sign in",
        )

    def build_fields(self, layout):
        self._add_field(layout, "full_name", "Full name",
                        placeholder="Jane Doe")
        self._add_field(layout, "email", "Email",
                        placeholder="you@example.com")
        self._add_field(layout, "password", "Password",
                        placeholder="At least 8 characters", password=True)

    def _validate(self):
        if len(self.fields["full_name"].text().strip()) < 2:
            raise ValueError("Please enter your full name.")
        if not _EMAIL_RE.match(self.fields["email"].text().strip()):
            raise ValueError("Please enter a valid email address.")
        if len(self.fields["password"].text()) < 8:
            raise ValueError("Password must be at least 8 characters.")

    def call_api(self):
        return api_client.register(
            self.fields["full_name"].text().strip(),
            self.fields["email"].text().strip(),
            self.fields["password"].text(),
        )


class AuthDialog(QDialog):
    """Modal onboarding dialog. Emits accepted() when a session is stored."""

    def __init__(self, start_on_register: bool = False):
        super().__init__()
        self.setWindowTitle("ExamAssist — Sign in")
        self.setModal(True)
        self.setFixedSize(420, 520)
        self.setStyleSheet(STYLE)
        self.setFont(QFont("Segoe UI", 10))
        # Normal, focusable window — no stealth flags here on purpose.
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

        self.stack = QStackedWidget()
        self.login = LoginForm()
        self.register_ = RegisterForm()
        self.stack.addWidget(self.login)
        self.stack.addWidget(self.register_)

        self.login.switch.connect(lambda: self.stack.setCurrentWidget(self.register_))
        self.register_.switch.connect(lambda: self.stack.setCurrentWidget(self.login))
        self.login.success.connect(self._store_and_accept)
        self.register_.success.connect(self._store_and_accept)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.stack)

        if start_on_register:
            self.stack.setCurrentWidget(self.register_)

    def _store_and_accept(self, payload: dict):
        token = payload.get("auth_token")
        user = payload.get("user") or {}
        tokens = payload.get("tokens")
        if not token:
            # defensive — surface a friendly message on either form
            active = self.stack.currentWidget()
            active._show_error("Unexpected response from server.")
            return
        session.set_auth(token, user, tokens)
        self.accept()


def run_auth_flow(parent=None) -> bool:
    """Show the auth dialog if there's no valid session. Returns True on success."""
    if session.is_authenticated():
        return True
    dlg = AuthDialog()
    return dlg.exec() == QDialog.Accepted
