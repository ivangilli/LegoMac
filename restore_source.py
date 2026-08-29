from pathlib import Path
import base64
import io
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parent
BUNDLE = ROOT / "source_bundle"
PARTS = ["part00.b64", "part01.b64", "part02.b64", "part03.b64"]

missing = [name for name in PARTS if not (BUNDLE / name).exists()]
if missing:
    raise SystemExit(f"Parti mancanti: {', '.join(missing)}")

encoded = "".join((BUNDLE / name).read_text(encoding="utf-8").strip() for name in PARTS)
raw = base64.b64decode(encoded, validate=True)

with zipfile.ZipFile(io.BytesIO(raw)) as archive:
    archive.testzip()
    archive.extractall(ROOT)

upgrade = ROOT / "upgrade_v11_6_b16.py"
if not upgrade.exists():
    raise SystemExit("upgrade_v11_6_b16.py mancante")
subprocess.run([sys.executable, str(upgrade)], cwd=ROOT, check=True)

print("LegoMac v11.6-MASTER-iPHONE-B16: sorgenti ripristinati correttamente.")
print("Ora puoi aprire la cartella in VS Code e avviare: python3 legofinderv7.py")
