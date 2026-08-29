from pathlib import Path
import py_compile

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "legofinderv7.py"

if not TARGET.exists():
    raise SystemExit("legofinderv7.py non trovato: esegui prima restore_source.py")

s = TARGET.read_text(encoding="utf-8")

replacements = [
    ('version = "v11.5-MASTER-A4-REMOTE"', 'version = "v11.6-MASTER-iPHONE-B16"'),
    ('master_remote_commands = []\n', 'master_remote_commands = []\nmaster_iphone_a4_status = {"plane": False, "reference": False, "message": "Mai richiesto"}\n'),
    ('        "a4_close": "Chiusura guida A4 richiesta",\n        "analyze": "Analisi richiesta",', '        "a4_close": "Chiusura guida A4 richiesta",\n        "a4_status": "Lettura stato calibrazione richiesta",\n        "analyze": "Analisi richiesta",'),
    ('win.geometry("620x560")', 'win.geometry("660x650")'),
    ('        ("4", "Chiudi guida e torna al riconoscimento", "a4_close",\n         "Calibrazione terminata: iPhone torna alla schermata principale."),\n    ]', '        ("4", "Leggi stato calibrazione iPhone", "a4_status",\n         "Richiedo all’iPhone lo stato attuale di piano, Plate 2×4 e calibrazione A4."),\n        ("5", "Chiudi guida e torna al riconoscimento", "a4_close",\n         "Calibrazione terminata: iPhone torna alla schermata principale."),\n    ]'),
    ('bg="#1565c0" if n=="1" else "#ef6c00" if n=="2" else "#c62828" if n=="3" else "#2e7d32",', 'bg="#1565c0" if n=="1" else "#ef6c00" if n=="2" else "#c62828" if n=="3" else "#455a64" if n=="4" else "#2e7d32",'),
    ('        if online:\n            stato.config(text="● iPhone collegato — pronto ai comandi remoti", fg="#2e7d32")\n        elif master_server is None:', '        if online:\n            a4 = master_iphone_a4_status\n            detail = str(a4.get("message", "")).strip()\n            base = "● iPhone collegato — build 16 remota pronta"\n            stato.config(text=base + (f"\\n{detail}" if detail else ""), fg="#2e7d32")\n        elif master_server is None:'),
    ('        global master_remote_commands\n        action = str(payload.get("action", "")).lower()\n        if action == "command_result":', '        global master_remote_commands, master_iphone_a4_status\n        action = str(payload.get("action", "")).lower()\n        if action == "command_result":'),
    ('            message = str(payload.get("message", "Comando completato"))\n            risultato.config(text=f"iPhone: {message}", fg="#2e7d32")\n            return {"ok": True, "command_id": command_id}', '            message = str(payload.get("message", "Comando completato"))\n            lower = message.lower()\n            if "piano=" in lower or "plate=" in lower or "calibrazione a4" in lower:\n                master_iphone_a4_status["message"] = message\n                if "piano=ok" in lower or "piano calibrato" in lower:\n                    master_iphone_a4_status["plane"] = True\n                if "plate=ok" in lower or ("plate 2×4" in lower and ("complet" in lower or "riconosci" in lower)):\n                    master_iphone_a4_status["reference"] = True\n            elif "piano" in lower and "calibr" in lower:\n                master_iphone_a4_status["plane"] = True\n                master_iphone_a4_status["message"] = message\n            elif "plate" in lower:\n                master_iphone_a4_status["message"] = message\n            risultato.config(text=f"iPhone B16: {message}", fg="#2e7d32")\n            return {"ok": True, "command_id": command_id, "a4_status": dict(master_iphone_a4_status)}'),
]

for old, new in replacements:
    if new in s:
        continue
    if old not in s:
        raise SystemExit(f"Patch v11.6 non applicabile: blocco non trovato: {old[:80]!r}")
    s = s.replace(old, new, 1)

TARGET.write_text(s, encoding="utf-8")
py_compile.compile(str(TARGET), doraise=True)
print("LegoMac v11.6-MASTER-iPHONE-B16 applicata e sintassi Python verificata.")
