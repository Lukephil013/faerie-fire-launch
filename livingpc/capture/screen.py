"""Screenshot + OCR collector.

grab() returns a PIL image of the primary monitor. save() writes it to the
rolling blob buffer. ocr() lazily loads RapidOCR; if it (or any dep) is missing,
OCR is silently disabled and the capturer still stores screenshots.

All heavy deps (mss, PIL, rapidocr) are imported lazily so the rest of the
package imports cleanly in environments where they aren't installed.
"""
from __future__ import annotations

import os
from io import BytesIO

from .. import crypto


class ScreenCapturer:
    def __init__(self, blob_dir: str, ocr_enabled: bool = True):
        self.blob_dir = blob_dir
        os.makedirs(blob_dir, exist_ok=True)
        self.ocr_enabled = ocr_enabled
        self._sct = None        # mss instance (lazy)
        self._ocr = None        # RapidOCR instance (lazy)
        self._ocr_failed = False

    # --- screenshot -------------------------------------------------------
    def _ensure_sct(self):
        if self._sct is None:
            import mss

            self._sct = mss.mss()
        return self._sct

    def grab(self):
        """Return a PIL.Image of the primary monitor."""
        from PIL import Image

        sct = self._ensure_sct()
        monitor = sct.monitors[1]  # [0] is the virtual all-monitors box; [1] primary
        shot = sct.grab(monitor)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    def save(self, image, ts: str) -> str:
        safe = ts.replace(":", "-").replace(".", "-")
        suffix = ".jpg.enc" if crypto.enabled() else ".jpg"
        path = os.path.join(self.blob_dir, f"{safe}{suffix}")
        buffer = BytesIO()
        image.save(buffer, "JPEG", quality=70)
        with open(path, "xb") as handle:
            handle.write(crypto.enc_bytes(buffer.getvalue()))
        return path

    # --- OCR --------------------------------------------------------------
    def ocr(self, image) -> str:
        if not self.ocr_enabled or self._ocr_failed:
            return ""
        try:
            engine = self._ensure_ocr()
            import numpy as np

            result, _ = engine(np.array(image))
            if not result:
                return ""
            return "\n".join(line[1] for line in result)
        except Exception:
            # Disable on first failure so we don't retry a broken import each tick.
            self._ocr_failed = True
            return ""

    def _ensure_ocr(self):
        if self._ocr is None:
            from rapidocr_onnxruntime import RapidOCR

            self._ocr = RapidOCR()
        return self._ocr
