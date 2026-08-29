"""Verifica il sorgente LegoMac dopo il clone.

Il sorgente completo è tracciato direttamente da Git: non servono bundle o
ricostruzioni Base64 che possano corrompersi durante il trasferimento.
"""

from pathlib import Path
import py_compile
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "legofinderv7.py"
SERVER = ROOT / "master_server.py"
UPGRADE = ROOT / "upgrade_v11_7_b17.py"

for required in (TARGET, SERVER, UPGRADE):
    if not required.is_file():
        raise SystemExit(f"File necessario mancante: {required.name}")

subprocess.run([sys.executable, str(UPGRADE)], cwd=ROOT, check=True)
py_compile.compile(str(TARGET), doraise=True)
py_compile.compile(str(SERVER), doraise=True)

print("LegoMac v11.7-MASTER-iPHONE-B17: sorgenti presenti e verificati.")
print("Avvio: python3 legofinderv7.py")
