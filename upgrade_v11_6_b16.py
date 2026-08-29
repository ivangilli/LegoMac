"""Verifica che il sorgente tracciato contenga il supporto iPhone B16."""

from pathlib import Path
import py_compile


ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "legofinderv7.py"
EXPECTED_VERSION = 'version = "v11.6-MASTER-iPHONE-B16"'
REQUIRED_MARKERS = (
    "master_iphone_a4_status",
    '"a4_start"',
    '"a4_plane"',
    '"a4_reference"',
    '"a4_status"',
    '"a4_close"',
    "apri_calibrazione_iphone_a4",
)

if not TARGET.is_file():
    raise SystemExit("legofinderv7.py non trovato")

source = TARGET.read_text(encoding="utf-8")
missing = [marker for marker in (EXPECTED_VERSION, *REQUIRED_MARKERS) if marker not in source]
if missing:
    raise SystemExit("Supporto v11.6/B16 incompleto: " + ", ".join(missing))

py_compile.compile(str(TARGET), doraise=True)
print("LegoMac v11.6/B16 presente; sintassi Python verificata.")
