"""Verifica il supporto iPhone / una webcam / due webcam della MASTER v12."""

from pathlib import Path
import py_compile


ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "legofinderv7.py"
CAMERA = ROOT / "camera_pipeline.py"
EXPECTED_VERSION = 'version = "v12.2-MASTER-STABLE-LINK-B3"'
REQUIRED_MARKERS = (
    "apri_sorgente_visiva",
    "calibra_sorgente_visiva",
    "analizza_sorgente_visiva",
    '("iphone", "iPhone"',
    '("camera1", "1 fotocamera"',
    '("camera2", "2 fotocamere"',
    "apri_calibrazione_iphone_a4",
    "fuse_predictions",
    "Avvia anteprima live",
    "/api/preview",
    "PreviewTransport",
)

for required in (TARGET, CAMERA):
    if not required.is_file():
        raise SystemExit(f"File necessario mancante: {required.name}")

source = TARGET.read_text(encoding="utf-8")
missing = [marker for marker in (EXPECTED_VERSION, *REQUIRED_MARKERS) if marker not in source]
if missing:
    raise SystemExit("Supporto v12 multicamera incompleto: " + ", ".join(missing))

py_compile.compile(str(TARGET), doraise=True)
py_compile.compile(str(CAMERA), doraise=True)
print("LegoMac v12 multicamera presente; sintassi Python verificata.")
