"""Acquisizione webcam e riconoscimento LEGO per LEGO Smista PRO.

Il modulo non importa tkinter: la UI può quindi usarlo da thread di lavoro e
testarlo senza aprire finestre. OpenCV è opzionale all'avvio dell'app e diventa
obbligatorio solo quando si seleziona una webcam.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
import json
import threading
import time

import requests

try:
    import cv2
    import numpy as np
except ImportError:  # l'app iPhone continua a funzionare senza OpenCV
    cv2 = None
    np = None


BRICKOGNIZE_URL = (
    "https://api.brickognize.com/predict/parts/"
    "?predict_color=false&top_k_items=20&min_similarity_items=0.35"
)


class CameraError(RuntimeError):
    """Errore comprensibile da mostrare direttamente nell'interfaccia."""


@dataclass(frozen=True)
class CameraChoice:
    index: int
    label: str


@dataclass
class CameraFrame:
    index: int
    frame_bgr: object
    cropped_bgr: object
    color_name: str


def require_opencv() -> None:
    if cv2 is None or np is None:
        raise CameraError(
            "Supporto webcam non installato. Esegui: "
            "python3 -m pip install -r requirements.txt"
        )


def discover_cameras(max_index: int = 8) -> list[CameraChoice]:
    """Sonda pochi indici in modo deterministico e rilascia sempre i device."""
    require_opencv()
    found: list[CameraChoice] = []
    for index in range(max_index):
        cap = cv2.VideoCapture(index)
        try:
            if not cap.isOpened():
                continue
            ok, frame = cap.read()
            if ok and frame is not None and frame.size:
                height, width = frame.shape[:2]
                found.append(CameraChoice(index, f"Fotocamera {index} — {width}×{height}"))
        finally:
            cap.release()
    return found


def normalize_part_id(value: str) -> str:
    value = str(value or "").strip().lower()
    for separator in ("/", "\\", "-"):
        if separator in value:
            value = value.split(separator, 1)[0]
    return value.removesuffix(".dat")


def ids_match(left: str, right: str) -> bool:
    a, b = normalize_part_id(left), normalize_part_id(right)
    return bool(a and b and (a == b or a.startswith(b + "c") or b.startswith(a + "c")))


def fuse_predictions(groups: Iterable[list[dict]], limit: int = 20) -> list[dict]:
    """Unisce le viste premiando i pezzi presenti in entrambe."""
    groups = list(groups)
    merged: dict[str, dict] = {}
    for view_index, rows in enumerate(groups):
        for rank, row in enumerate(rows):
            part_id = normalize_part_id(row.get("id", ""))
            if not part_id:
                continue
            score = float(row.get("score", 0.0))
            weighted = score * (1.0 - min(rank, 10) * 0.015)
            current = merged.setdefault(part_id, {
                **row, "id": part_id, "score": 0.0, "views": 0, "best_score": 0.0,
            })
            current["score"] += weighted
            current["views"] += 1
            current["best_score"] = max(current["best_score"], score)
            if not current.get("img_url") and row.get("img_url"):
                current["img_url"] = row["img_url"]
            if not current.get("name") and row.get("name"):
                current["name"] = row["name"]

    view_count = max(1, len(groups))
    for row in merged.values():
        average = row["score"] / max(1, row["views"])
        agreement = 0.10 if view_count > 1 and row["views"] > 1 else 0.0
        row["score"] = min(0.999, average + agreement)
    return sorted(merged.values(), key=lambda row: (-row["score"], -row["views"], row["id"]))[:limit]


def predict_jpeg(jpeg: bytes, timeout: int = 25) -> list[dict]:
    response = requests.post(
        BRICKOGNIZE_URL,
        files={"query_image": ("piece.jpg", jpeg, "image/jpeg")},
        headers={"Accept": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    return list(payload.get("items") or [])


def estimate_color(frame_bgr) -> str:
    """Classificazione volutamente coerente con quella usata dall'iPhone."""
    if frame_bgr is None or not frame_bgr.size:
        return "Unknown"
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB).reshape(-1, 3)
    # Scarta il 15% più scuro/chiaro: riduce sfondo e riflessi.
    brightness = rgb.mean(axis=1)
    keep = (brightness >= np.percentile(brightness, 15)) & (brightness <= np.percentile(brightness, 85))
    if keep.any():
        rgb = rgb[keep]
    r, g, b = np.median(rgb, axis=0).astype(float)
    hi, lo = max(r, g, b), min(r, g, b)
    if hi < 48: return "Black"
    if lo > 185 and hi - lo < 35: return "White"
    if hi - lo < 24: return "Dark Bluish Gray" if hi < 120 else "Light Bluish Gray"
    if r > g * 1.32 and r > b * 1.32: return "Orange" if g > 105 else "Red"
    if b > r * 1.18 and b > g * 1.10: return "Blue"
    if g > r * 1.12 and g > b * 1.05: return "Green"
    if r > 155 and g > 135 and b < 110: return "Yellow"
    return "Unknown"


class CameraSession:
    """Gestisce una o due webcam, piano negativo e scatto simultaneo."""

    def __init__(self, indices: list[int], calibration_dir: str | Path):
        require_opencv()
        if not 1 <= len(indices) <= 2:
            raise CameraError("Seleziona una o due fotocamere.")
        if len(set(indices)) != len(indices):
            raise CameraError("Nella modalità a due viste scegli due fotocamere diverse.")
        self.indices = [int(i) for i in indices]
        self.calibration_dir = Path(calibration_dir)
        self.calibration_dir.mkdir(parents=True, exist_ok=True)
        self.captures: list = []
        self.backgrounds: dict[int, object] = {}
        self._lock = threading.Lock()
        self._load_backgrounds()

    def open(self) -> None:
        self.close()
        opened = []
        try:
            for index in self.indices:
                cap = cv2.VideoCapture(index)
                if not cap.isOpened():
                    cap.release()
                    raise CameraError(f"Fotocamera {index} non disponibile o già utilizzata.")
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
                opened.append(cap)
            self.captures = opened
            time.sleep(0.15)
        except Exception:
            for cap in opened:
                cap.release()
            raise

    def close(self) -> None:
        with self._lock:
            for cap in self.captures:
                cap.release()
            self.captures = []

    def _read_raw(self) -> list[tuple[int, object]]:
        if not self.captures:
            raise CameraError("Apri prima le fotocamere.")
        rows = []
        with self._lock:
            for _ in range(3):  # svuota frame vecchi dai buffer USB
                for cap in self.captures:
                    cap.grab()
            for index, cap in zip(self.indices, self.captures):
                ok, frame = cap.retrieve()
                if not ok or frame is None:
                    ok, frame = cap.read()
                if not ok or frame is None:
                    raise CameraError(f"Nessuna immagine dalla fotocamera {index}.")
                rows.append((index, frame))
        return rows

    def preview(self) -> list[CameraFrame]:
        return [self._prepare(index, frame) for index, frame in self._read_raw()]

    def calibrate_plane(self) -> None:
        frames = self._read_raw()
        for index, frame in frames:
            self.backgrounds[index] = frame.copy()
            cv2.imwrite(str(self.calibration_dir / f"camera_{index}_plane.jpg"), frame)

    def has_plane(self, index: int) -> bool:
        return index in self.backgrounds

    def _load_backgrounds(self) -> None:
        for index in self.indices:
            path = self.calibration_dir / f"camera_{index}_plane.jpg"
            if path.exists():
                frame = cv2.imread(str(path))
                if frame is not None:
                    self.backgrounds[index] = frame

    def _prepare(self, index: int, frame) -> CameraFrame:
        cropped = frame
        background = self.backgrounds.get(index)
        if background is not None and background.shape == frame.shape:
            delta = cv2.absdiff(frame, background)
            gray = cv2.cvtColor(delta, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (7, 7), 0)
            _, mask = cv2.threshold(gray, 28, 255, cv2.THRESH_BINARY)
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8), iterations=2)
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if contours:
                height, width = frame.shape[:2]
                cx, cy = width / 2, height / 2
                viable = [c for c in contours if cv2.contourArea(c) >= width * height * 0.001]
                if viable:
                    def priority(contour):
                        x, y, w, h = cv2.boundingRect(contour)
                        distance = ((x + w / 2 - cx) ** 2 + (y + h / 2 - cy) ** 2) ** 0.5
                        return cv2.contourArea(contour) - distance * 5
                    x, y, w, h = cv2.boundingRect(max(viable, key=priority))
                    pad = max(24, int(max(w, h) * 0.20))
                    x0, y0 = max(0, x - pad), max(0, y - pad)
                    x1, y1 = min(width, x + w + pad), min(height, y + h + pad)
                    cropped = frame[y0:y1, x0:x1]
        return CameraFrame(index, frame, cropped, estimate_color(cropped))

    @staticmethod
    def jpeg(frame, quality: int = 90) -> bytes:
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise CameraError("Impossibile codificare l'immagine della fotocamera.")
        return encoded.tobytes()

    def recognize(self, progress: Callable[[str], None] | None = None) -> tuple[list[dict], list[CameraFrame]]:
        frames = self.preview()
        groups = []
        for pos, frame in enumerate(frames, 1):
            if progress:
                progress(f"Brickognize: analizzo vista {pos}/{len(frames)}…")
            groups.append(predict_jpeg(self.jpeg(frame.cropped_bgr)))
        return fuse_predictions(groups), frames


def save_camera_config(path: str | Path, mode: str, indices: list[int]) -> None:
    Path(path).write_text(json.dumps({"mode": mode, "indices": indices}, indent=2), encoding="utf-8")


def load_camera_config(path: str | Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        mode = value.get("mode", "iphone")
        indices = [int(i) for i in value.get("indices", [0, 1])]
        return {"mode": mode if mode in {"iphone", "camera1", "camera2"} else "iphone", "indices": indices}
    except (OSError, ValueError, TypeError):
        return {"mode": "iphone", "indices": [0, 1]}
