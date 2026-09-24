"""Fullscreen frozen-shot dimmer + rubber-band region selection.
Opaque window showing a dimmed LIVE SNAPSHOT of the screen — a per-pixel
translucent fullscreen window fails SetWindowDisplayAffinity on some
GPUs (error 8), an opaque one is proven to work.
Invisible to capture (WDA_EXCLUDEFROMCAPTURE), never steals focus."""
import sys
import logging

from PySide6.QtCore import Qt, QRect, QTimer, Signal
from PySide6.QtGui import (QGuiApplication, QColor, QPainter, QPen,
                            QCursor, QFont)
from PySide6.QtWidgets import QWidget

import winapi

DIM_COLOR   = QColor(0, 0, 0, 110)
SEL_BORDER  = QColor("#4da3ff")
MIN_SIZE    = 16
HINT_TEXT   = "Drag to select the question area   |   Right-click to cancel"


class RegionSelector(QWidget):
    # emits (QRect or None): global logical rect, or None if cancelled
    finished = Signal(object)
    # emits a user-facing message (shows on the panel)
    status = Signal(str)

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint
                            | Qt.WindowStaysOnTopHint
                            | Qt.Tool
                            | Qt.WindowDoesNotAcceptFocus)
        # NOTE: no WA_TranslucentBackground — that was the affinity-killer
        # self.setCursor(Qt.CrossCursor)

        self._origin = None
        self._current = None
        self._frozen = None      # live snapshot of the screen, taken in start()

    # ---------- lifecycle ----------
    def start(self):
        if self.isVisible():
            return
        screen = (QGuiApplication.screenAt(QCursor.pos())
                  or QGuiApplication.primaryScreen())

        # 1) freeze the live screen BEFORE any of our windows are visible
        #    (our panel is affinity-excluded, so it won't appear in this
        #     snapshot either — no dimmer-visible loops)
        self._frozen = screen.grabWindow(0)

        # 2) show an OPAQUE window covering that screen, painted with the
        #    dimmed snapshot
        self.setGeometry(screen.geometry())
        self._origin = None
        self._current = None

        self.show()
        if sys.platform == "win32":
            winapi.show_without_activation(self)
        self.raise_()

        if sys.platform == "win32":
            winapi.apply_stealth(self, click_through=False)
            # re-assert after DWM present; retry in case of transient failure
            QTimer.singleShot(100,  self._late_stealth)
            QTimer.singleShot(400,  self._late_stealth)
            QTimer.singleShot(1200, self._late_stealth)

    def _late_stealth(self):
        if not self.isVisible():
            return
        if not winapi.is_hidden_from_capture(self):
            winapi.apply_stealth(self, click_through=False)
        if not winapi.is_hidden_from_capture(self):
            logging.warning("[selector] still not capture-excluded!")
        else:
            logging.info("[selector] hidden-from-capture: True")


    # ---------- mouse ----------
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._origin = e.globalPosition().toPoint()
            self._current = QRect(self._origin, self._origin)
            self.grabMouse()
            self.update()
        elif e.button() == Qt.RightButton:
            self._cancel()

    def mouseMoveEvent(self, e):
        if self._origin is not None:
            self._current = QRect(self._origin,
                                  e.globalPosition().toPoint()).normalized()
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._origin is not None:
            self.releaseMouse()
            rect = QRect(self._origin,
                         e.globalPosition().toPoint()).normalized()
            self._origin = None
            self._current = None
            self._done(rect)
        elif e.button() == Qt.RightButton:
            self.releaseMouse()

    # ---------- completion ----------
    def _release_frozen(self):
        self._frozen = None        # free ~10-30 MB immediately

    def _cancel(self, message: str = "Selection cancelled."):
        self._origin = None
        self._current = None
        self._release_frozen()
        self.hide()
        self.status.emit(message)
        self.finished.emit(None)

    def _done(self, rect):
        if rect.width() < MIN_SIZE or rect.height() < MIN_SIZE:
            self._cancel("Too small to read — drag a wider/taller box.")
            return
        if not winapi.is_hidden_from_capture(self):
            logging.warning("[selector] exclusion not active at capture time!")
        self._release_frozen()
        self.hide()
        if rect.height() < 40:
            # thin but usable — OCR preprocessing pads/upscales it,
            # but tell the user so they can re-select taller next time
            self.status.emit("Thin selection — working, but a taller "
                             "box reads more reliably.")
        self.finished.emit(rect)

    # ---------- painting ----------
    def paintEvent(self, event):
        p = QPainter(self)

        # dimmed live snapshot as the base layer
        if self._frozen is not None:
            p.drawPixmap(self.rect(), self._frozen)
        p.fillRect(self.rect(), DIM_COLOR)

        if self._current is not None and not self._current.isNull():
            local = self._current.translated(-self.geometry().topLeft())

            # punch-out: redraw the ORIGINAL pixels inside the selection
            if self._frozen is not None:
                p.save()
                p.setClipRect(local)
                p.drawPixmap(self.rect(), self._frozen)
                p.restore()

            p.setPen(QPen(SEL_BORDER, 2))
            p.drawRect(local)
        else:
            p.setPen(QColor(220, 220, 220))
            p.setFont(QFont("Segoe UI", 10))
            p.drawText(self.rect().adjusted(0, 40, 0, 0),
                       Qt.AlignHCenter | Qt.AlignTop, HINT_TEXT)
