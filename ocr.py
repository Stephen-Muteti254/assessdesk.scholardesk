"""RapidOCR wrapper: QPixmap in -> cleaned question text out.
Engine loads lazily (first call ~1-2 s); preload() warms it at startup.
Runs entirely offline — zero network footprint."""
import sys
import logging

import numpy as np
from PySide6.QtGui import (
    QImage, QPixmap, QGuiApplication
)
import re
import config
import os

_engine = None
MIN_CONFIDENCE = 0.50   # drop junk boxes below this score


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        
        # Pass configuration variables that do not break the path lookup mapping
        # Let the library naturally fall back to its internal default directory paths
        _engine = RapidOCR(box_thresh=MIN_CONFIDENCE) 
    return _engine


def preload():
    """Warm up the model at app start so the first snip isn't slow."""
    _get_engine()


def _pixmap_to_ndarray(pix) -> np.ndarray:
    """QPixmap -> BGR ndarray (OpenCV convention), full device resolution."""
    if pix.devicePixelRatio() != 1.0:
        # captures arrive with DPR set; toImage() yields device pixels,
        # but be explicit: strip the logical scaling
        img = pix.toImage()
    else:
        img = pix.toImage()
    img = img.convertToFormat(QImage.Format_BGR888)

    h, w = img.height(), img.width()
    ptr = img.constBits()
    arr = np.frombuffer(ptr, dtype=np.uint8,
                        count=img.sizeInBytes()).reshape(h, img.bytesPerLine())
    # drop per-row padding, keep exact width, 3 channels
    arr = arr[:, :w * 3].reshape(h, w, 3)
    return arr.copy()   # copy: constBits() buffer is owned by QImage


def _preprocess(arr: np.ndarray) -> np.ndarray:
    """Precondition captures for PP-OCR:
    1. invert dark-mode captures (detector wants dark-on-light),
    2. upscale aggressively when the capture is small,
    3. pad a white border — the detector drops text boxes that
       touch the image edge (thin single-line selections!)."""
    import cv2

    # 1) dark-mode polarity fix
    gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
    if float(gray.mean()) < 110:
        arr = 255 - arr
        logging.info("[ocr] dark-mode capture inverted")

    # 2) thin captures need more than the global upscale
    h, w = arr.shape[:2]
    scale = 3 if min(h, w) < 120 else config.OCR_UPSCALE
    if scale != 1:
        arr = cv2.resize(arr, None, fx=scale, fy=scale,
                         interpolation=cv2.INTER_CUBIC)
        if scale != config.OCR_UPSCALE:
            logging.info("[ocr] small capture (%dx%d) upscaled %dx", w, h, scale)

    # 3) white border so edge-touching text survives detection
    arr = cv2.copyMakeBorder(arr, 30, 30, 30, 30,
                            cv2.BORDER_CONSTANT, value=(255, 255, 255))
    return arr


def _clean_lines(raw) -> list:
    """RapidOCR raw output -> real text lines.

    RapidOCR often returns WORD-level boxes on screen text. We use each
    box's coordinates to reconstruct lines: words whose boxes overlap
    vertically belong to the same line; within a line, sort left-to-right
    and join with single spaces.
    """
    if not raw:
        return []

    # --- 1. collect valid words with their geometry ---
    words = []
    for item in raw:
        try:
            box, text, score = _normalize_item(item)
        except (ValueError, TypeError, IndexError):
            continue
        if score is not None and score < MIN_CONFIDENCE:
            continue
        text = (text or "").strip()
        if not text:
            continue
        try:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            top, bottom = min(ys), max(ys)
            left = min(xs)
        except (TypeError, IndexError):
            # no usable geometry — treat as its own line
            words.append((0, 0, 0, text))
            continue
        words.append((top, bottom, left, text))

    if not words:
        return []

    # --- 2. cluster into lines by vertical overlap ---
    words.sort(key=lambda w: (w[0], w[2]))          # top, then left
    lines = []
    cur_line = [words[0]]
    for w in words[1:]:
        prev = cur_line[-1]
        prev_height = max(prev[1] - prev[0], 8)      # guard tiny boxes
        # same line if this word's top starts before the previous
        # word's box is half-closed vertically
        if w[0] < prev[0] + 0.6 * prev_height:
            cur_line.append(w)
        else:
            lines.append(cur_line)
            cur_line = [w]
    lines.append(cur_line)

    # --- 3. join each line: sort left-to-right, single spaces ---
    result = []
    for line in lines:
        line.sort(key=lambda w: w[2])                # sort left-to-right by 'left' coordinate
        
        words_text = []
        for word in line:
            word_text = word[3]
            
            # Regex Fix: Add spaces between lowercase letters followed by uppercase letters 
            # (Transforms 'onanMcQcapture' into 'onan Mc Qcapture')
            word_text = re.sub(r'([a-z])([A-Z])', r'\1 \2', word_text)
            
            # Regex Fix: Add spaces between text characters and standalone symbols/operators
            # (Transforms 'question+options' into 'question + options')
            word_text = re.sub(r'([a-zA-Z0-9])([=+→\-:/])', r'\1 \2', word_text)
            word_text = re.sub(r'([=+→\-:/])([a-zA-Z0-9])', r'\1 \2', word_text)
            
            words_text.append(word_text)
            
        # Join words with spaces, then collapse any double spaces down to a single space
        combined_line = " ".join(words_text)
        combined_line = re.sub(r'\s+', ' ', combined_line).strip()
        result.append(combined_line)
        
    return result


def _normalize_item(item):
    """Return (box, text, score-or-None) from any known result shape."""
    # shape 1: [box, (text, score)]
    if len(item) == 2 and isinstance(item[1], (tuple, list)):
        box = item[0]
        text, score = item[1]
        return box, str(text), _to_float(score)

    # shape 2/3: [box, text, score]
    box, text = item[0], item[1]
    score = _to_float(item[2]) if len(item) >= 3 else None
    return box, str(text), score


def _to_float(v):
    try:
        return float(v)                 # handles "0.98" and 0.98 alike
    except (TypeError, ValueError):
        return None                     # unparseable -> treat as "keep"


def extract_text(pix) -> str:
    """Full pipeline: pixmap -> cleaned multi-line question text."""
    engine = _get_engine()
    arr = _preprocess(_pixmap_to_ndarray(pix))
    raw, _elapse = engine(arr)
    lines = _clean_lines(raw)
    return "\n".join(lines)


# ---------- standalone validation (Milestone 4) ----------
if __name__ == "__main__":
    """Usage: python ocr.py last_capture.png
    Run OCR on a saved capture WITHOUT starting the whole app."""

    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(levelname)s %(message)s")

    path = sys.argv[1] if len(sys.argv) > 1 else "last_capture.png"
    if not os.path.exists(path):
        print(f"no such file: {path}")
        sys.exit(1)

    app = QGuiApplication([])
    print("[ocr] loading engine (first run takes a few seconds)...")
    preload()
    text = extract_text(QPixmap(path))
    print("=" * 60)
    print(text if text else "(no text found)")
    print("=" * 60)
    print(f"[ocr] {len(text)} chars, {len(text.splitlines())} lines")
