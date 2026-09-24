"""Grab a global-rectangle region of the screen, DPI-aware."""
from PySide6.QtCore import QRect, QPoint
from PySide6.QtGui import QGuiApplication

def capture_region(rect: QRect):
    """Capture `rect` (global, logical coordinates) and return a QPixmap."""
    screen = (QGuiApplication.screenAt(QPoint(rect.x(), rect.y()))
              or QGuiApplication.primaryScreen())
    dpr = screen.devicePixelRatio()
    geo = screen.geometry()

    # full shot of that screen (device pixels)
    full = screen.grabWindow(0)

    # convert global logical rect -> device pixels relative to this screen
    x = int((rect.x() - geo.x()) * dpr)
    y = int((rect.y() - geo.y()) * dpr)
    w = int(rect.width() * dpr)
    h = int(rect.height() * dpr)

    return full.copy(x, y, w, h)
