# =============================================================================
# CHANGELOG
# =============================================================================
# build.8 - Filtro Tipo per tipologia pezzo (Brick, Plate, Tile, Slope, Technic, ecc.)
# build.7 - LRU image cache (max 300 slot, evict oldest); Save JSON asincrono + backup .bak
#           Undo ultimo click: Ctrl+Z + bottone toolbar; Filtro testuale libero griglia
#           Barra progresso pezzi (unicode) per set selezionato
# build.6 - Fix cache dropdown Set: confronta solo nomi set (non label con contatori)
#           Aggiorna label set selezionato dopo ogni click su un pezzo
# build.5 - Elenco a discesa Set: mostra [usati/totali] pezzi vicino al nome set
# build.3 - Cache render griglia: salta sort/filter se filtri non cambiati
#           Debounce filtri 150ms: griglia statica se mouse passa su dropdown
#           Fix bold_font: stessa grandezza di normal_font (era +1 size)
# build.2 - Cache dropdown Colore e Set: non rigenera menu se lista invariata
# build.1 - Versione base (prima delle ottimizzazioni performance)
# =============================================================================

import tkinter as tk
from PIL import Image, ImageTk, ImageDraw, ImageFont
import requests
from io import BytesIO
import json
import os
import threading
import re
import shutil
import sys
import subprocess
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from tkinter import messagebox, ttk
import functools
import time
import collections
import secrets
import queue
from datetime import datetime
from html import escape

from camera_pipeline import (
    CameraError, CameraSession, discover_cameras, fuse_predictions,
    ids_match, load_camera_config, save_camera_config,
)

def load_rebrickable_api_key():
    """Carica la chiave dall'ambiente o dalla configurazione locale esclusa da Git."""
    env_key = os.environ.get("REBRICKABLE_API_KEY", "").strip()
    if env_key:
        return env_key
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "local_config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as config_file:
            return str(json.load(config_file).get("rebrickable_api_key", "")).strip()
    except (OSError, ValueError, TypeError):
        return ""


API_KEY = load_rebrickable_api_key()
ICON_SIZE = 140
MAX_THREADS = 3
version = "v12.2-MASTER-STABLE-LINK-B3"
SAVE_COOLDOWN = 2  # secondi tra salvataggi batch
MAX_IMAGE_CACHE_SIZE = 300  # max immagini in cache LRU

colonne = 18
sets = {}
set_codes = {}
global_parts = {}
image_cache = collections.OrderedDict()  # LRU cache
ui_refs = {}
ultimo_set_modificato = None
ultimo_movimento = ""
numerazione_set = {}
aggiorna_lista_func = None
last_selected = None
last_save_time = 0  # per batch save
save_pending = False  # flag per salvataggio in sospeso
save_lock = threading.Lock()
save_thread = None

script_dir = os.path.dirname(os.path.abspath(__file__))
mysets_file = os.path.join(script_dir, "mysets.json")
lego_data_file = os.path.join(script_dir, "lego_data.json")
image_dir = os.path.join(script_dir, "images")
set_thumb_dir = os.path.join(image_dir, "set_thumbs")
backup_dir = os.path.join(script_dir, "backups")
version_info_file = os.path.join(script_dir, "app_version.json")
ui_settings_file = os.path.join(script_dir, "ui_settings.json")
master_config_file = os.path.join(script_dir, "master_config.json")
piece_dimensions_file = os.path.join(script_dir, "piece_dimensions.json")
camera_config_file = os.path.join(script_dir, "camera_config.json")
camera_calibration_dir = os.path.join(script_dir, "camera_calibration")
os.makedirs(image_dir, exist_ok=True)
os.makedirs(set_thumb_dir, exist_ok=True)
os.makedirs(backup_dir, exist_ok=True)

gestione_win = None
stato_pezzo_win = None
disabled_sets = set()          # nomi set disabilitati (saltati da filtri e griglia)
stato_pezzo_key = None
apri_dettaglio_set_func = None
anteprima_set_last_position = None
anteprima_set_win = None
anteprima_set_ctx = None
anteprima_set_load_token = 0

# Database dimensioni pezzi (scaricato da lego_bricklink_scraper.py)
def _load_piece_dimensions():
    if not os.path.exists(piece_dimensions_file):
        return {}
    try:
        with open(piece_dimensions_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if not k.startswith("__")}
    except Exception:
        return {}

piece_dimensions_db = _load_piece_dimensions()

DEFAULT_UI_SETTINGS = {
    "color": "Tutti",
    "set_name": None,
    "piece_type": "Tutti",
    "solo_mancanti": False,
    "togli_completi": False,
    "togli_zero": False,
    "lock": False,
    "colonne": 18,
    "icon_size": 140,
    "sort_mode": "Stud"
}
ui_settings = DEFAULT_UI_SETTINGS.copy()
pending_restore_saved_filters = True
master_server = None
preview_transport = None
master_pairing_info = None
master_ui_requests = queue.Queue()
import_ui_requests = queue.Queue()
master_remote_commands = []
master_iphone_a4_status = {
    "plane": False,
    "reference": False,
    "message": "Stato calibrazione non ancora richiesto",
}
master_next_command_id = 1
master_iphone_last_seen = None
camera_config = load_camera_config(camera_config_file)
camera_session = None
camera_window = None


def load_master_config():
    """Carica porta/PIN o li crea al primo avvio senza alterare i dati LEGO."""
    config = {"enabled": True, "port": 8765, "pin": f"{secrets.randbelow(1_000_000):06d}"}
    try:
        if os.path.exists(master_config_file):
            with open(master_config_file, "r", encoding="utf-8") as f:
                config.update(json.load(f))
        else:
            with open(master_config_file, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
    except Exception as exc:
        print(f"[MASTER] Configurazione non salvata: {exc}")
    config["port"] = int(config.get("port", 8765))
    config["pin"] = str(config.get("pin", "000000")).zfill(6)[-6:]
    return config

# ------------------------
# API
# ------------------------
def get_set_info(set_code):
    try:
        r = requests.get(
            f"https://rebrickable.com/api/v3/lego/sets/{set_code}/",
            headers={"Authorization": f"key {API_KEY}"},
            timeout=5
        )
        if r.status_code == 200:
            data = r.json()
            img_url = data.get("set_img_url") or ""
            if img_url:
                set_img_url_mem[set_code] = img_url
            return data.get("name"), True
        return None, False
    except:
        return None, False

def scarica_set(set_code):
    parts = {}
    page = 1
    while True:
        try:
            r = requests.get(
                f"https://rebrickable.com/api/v3/lego/sets/{set_code}/parts/",
                headers={"Authorization": f"key {API_KEY}"},
                params={"page": page},
                timeout=5
            )
            data = r.json()
        except:
            break

        for item in data.get("results", []):
            if item.get("is_spare"):
                continue
            key = f"{item['part']['part_num']}_{item['color']['name']}"
            qty = item["quantity"]
            if key not in parts:
                parts[key] = {
                    "total": qty,
                    "used": 0,
                    "img": item["part"]["part_img_url"],
                    "name": item["part"]["name"]
                }
            else:
                parts[key]["total"] += qty

        if not data.get("next"):
            break
        page += 1

    return parts

# ------------------------
# FILE JSON
# ------------------------

OFFLINE_FILE = os.path.join(script_dir, "sets_offline.json")
OFFLINE_CACHE_VERSION = 2

def load_offline_sets():
    """Carica il file dei set offline, se esiste."""
    if os.path.exists(OFFLINE_FILE):
        with open(OFFLINE_FILE, "r") as f:
            data = json.load(f)
        if data.get("cache_version") != OFFLINE_CACHE_VERSION:
            print(
                f"[LOAD] Cache offline obsoleta: rigenero '{OFFLINE_FILE}' senza spare parts."
            )
            return {
                "cache_version": OFFLINE_CACHE_VERSION,
                "set_codes": {},
                "parts": {}
            }
        print(f"[LOAD] Caricato {len(data.get('set_codes', {}))} set dal file offline '{OFFLINE_FILE}'")
        return data
    else:
        print(f"[LOAD] Nessun file offline trovato. Inizio con dati vuoti.")
        return {
            "cache_version": OFFLINE_CACHE_VERSION,
            "set_codes": {},
            "parts": {}
        }

def save_offline_sets(data):
    """Salva il file dei set offline."""
    data["cache_version"] = OFFLINE_CACHE_VERSION
    with open(OFFLINE_FILE, "w") as f:
        json.dump(data, f, indent=4)
    print(f"[SAVE] Salvati {len(data.get('set_codes', {}))} set nel file offline '{OFFLINE_FILE}'")

def ensure_version_backup():
    """Crea un backup incrementale del file quando cambia la versione dell'app."""
    info = {"last_version": None, "history": []}

    if os.path.exists(version_info_file):
        try:
            with open(version_info_file, "r") as f:
                info = json.load(f)
        except Exception:
            info = {"last_version": None, "history": []}

    if info.get("last_version") == version:
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = os.path.splitext(os.path.basename(__file__))[0]
    backup_name = f"{base_name}_{version}_{timestamp}.py"
    backup_path = os.path.join(backup_dir, backup_name)

    try:
        shutil.copy2(__file__, backup_path)
        history = info.get("history", [])
        history.append({
            "version": version,
            "backup_file": backup_name,
            "created_at": timestamp
        })
        with open(version_info_file, "w") as f:
            json.dump({
                "last_version": version,
                "history": history
            }, f, indent=2)
        print(f"[BACKUP] Creato backup versione {version}: {backup_name}")
    except Exception as e:
        print(f"[ERRORE] Backup versione non riuscito: {e}")

def load_ui_settings():
    if not os.path.exists(ui_settings_file):
        return DEFAULT_UI_SETTINGS.copy()

    try:
        with open(ui_settings_file, "r") as f:
            loaded = json.load(f)
            if not isinstance(loaded, dict):
                return DEFAULT_UI_SETTINGS.copy()

            merged = DEFAULT_UI_SETTINGS.copy()
            merged.update(loaded)
            return merged
    except Exception:
        return DEFAULT_UI_SETTINGS.copy()

def save_ui_settings():
    try:
        selected_set_name = get_selected_set_name() if set_var is not None else None
        data = {
            "color": color_var.get() if color_var is not None else "Tutti",
            "set_name": selected_set_name,
            "piece_type": piece_type_var.get() if piece_type_var is not None else "Tutti",
            "solo_mancanti": bool(solo_mancanti_var.get()) if solo_mancanti_var is not None else False,
            "togli_completi": bool(togli_completi_var.get()) if togli_completi_var is not None else False,
            "togli_zero": bool(togli_zero_var.get()) if togli_zero_var is not None else False,
            "lock": bool(lock_var.get()) if lock_var is not None else False,
            "colonne": int(colonne),
            "icon_size": int(ICON_SIZE),
            "sort_mode": ordine_var.get() if ordine_var is not None else "Stud"
        }
        with open(ui_settings_file, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[ERRORE] Salvataggio impostazioni UI: {e}")

def applica_filtri_salvati():
    global pending_restore_saved_filters
    if not pending_restore_saved_filters:
        return

    pending_restore_saved_filters = False

    saved_set_name = ui_settings.get("set_name")
    saved_color = ui_settings.get("color", "Tutti")

    if saved_set_name and saved_set_name in sets:
        set_label = format_set_filter_label(saved_set_name)
        on_set_filter_change(set_label)
    else:
        on_set_filter_change("Tutti")

    if color_var is not None and saved_color in colori_disponibili:
        on_color_filter_change(saved_color)
    else:
        on_color_filter_change("Tutti")

    aggiorna_griglia()

def auto_resize_window(
    win,
    content_widget,
    scrollbar=None,
    min_w=420,
    min_h=220,
    max_w=1500,
    max_h=900,
    extra_w=30,
    extra_h=80
):
    """Ridimensiona automaticamente una finestra in base al contenuto."""
    try:
        if win is None or not win.winfo_exists():
            return
        if content_widget is None or not content_widget.winfo_exists():
            return

        win.update_idletasks()

        scroll_w = 0
        if scrollbar is not None and scrollbar.winfo_exists():
            scroll_w = scrollbar.winfo_reqwidth()

        width = min(
            max(min_w, content_widget.winfo_reqwidth() + scroll_w + extra_w),
            max_w
        )
        height = min(
            max(min_h, content_widget.winfo_reqheight() + extra_h),
            max_h
        )
        win.geometry(f"{width}x{height}")
    except Exception:
        pass

def bind_mousewheel_scroll(canvas_widget, on_scroll=None):
    """Abilita scroll rotella/trackpad sul canvas sotto il cursore (macOS incluso)."""
    def _get_delta(event):
        # Linux/X11 fallback (Button-4 su, Button-5 giu)
        num = getattr(event, "num", None)
        if num == 4:
            return 120
        if num == 5:
            return -120
        delta = getattr(event, "delta", 0)
        return delta if delta is not None else 0

    def on_mousewheel(event):
        try:
            delta = _get_delta(event)
            if delta == 0:
                return

            # Modalita stabile: usa yview_scroll a unita, evitando anomalie di direzione su trackpad.
            if sys.platform == "darwin":
                step = -1 if delta > 0 else 1
                canvas_widget.yview_scroll(step, "units")
            else:
                if abs(delta) >= 120:
                    step = int(-delta / 120)
                else:
                    step = -1 if delta > 0 else 1
                if step != 0:
                    canvas_widget.yview_scroll(step, "units")

            if on_scroll:
                on_scroll()
        except Exception:
            pass

    def bind_to_canvas(_event):
        canvas_widget.bind_all("<MouseWheel>", on_mousewheel)
        canvas_widget.bind_all("<Button-4>", on_mousewheel)
        canvas_widget.bind_all("<Button-5>", on_mousewheel)

    def unbind_from_canvas(_event):
        canvas_widget.unbind_all("<MouseWheel>")
        canvas_widget.unbind_all("<Button-4>")
        canvas_widget.unbind_all("<Button-5>")

    canvas_widget.bind("<Enter>", bind_to_canvas)
    canvas_widget.bind("<Leave>", unbind_from_canvas)

def bind_right_click(widget, callback):
    """Bind click destro in modo compatibile (trackpad macOS incluso)."""
    if sys.platform == "darwin":
        # Su macOS il secondary click del trackpad puo arrivare come Button-2.
        widget.bind("<Button-2>", callback)
        widget.bind("<Button-3>", callback)
    else:
        widget.bind("<Button-3>", callback)

def load_mysets():
    if os.path.exists(mysets_file):
        try:
            return json.load(open(mysets_file))
        except:
            return []
    return []

def save_mysets(codes):
    json.dump(codes, open(mysets_file, "w"))

def esporta_foglio_etichette_set_a4():
    if not sets:
        messagebox.showinfo("Stampa Set", "Nessun set caricato!")
        return

    ordinati = sorted(sets.keys(), key=lambda nome: numerazione_set.get(nome, 9999))
    labels = []
    for nome in ordinati:
        numero = numerazione_set.get(nome, "?")
        labels.append((str(numero), nome))

    if not labels:
        messagebox.showinfo("Stampa Set", "Nessun set numerato disponibile.")
        return

    # PDF A4 a 300 DPI: 2480x3508 px
    dpi = 300
    page_w, page_h = 2480, 3508
    margin = int(round((5 / 25.4) * dpi))  # 5 mm
    cols = 3
    rows = 6
    per_page = cols * rows

    grid_w = page_w - (2 * margin)
    grid_h = page_h - (2 * margin)
    cell_w = grid_w // cols
    cell_h = grid_h // rows
    grid_w = cell_w * cols
    grid_h = cell_h * rows
    x0 = (page_w - grid_w) // 2
    y0 = (page_h - grid_h) // 2

    def load_font(candidates, size):
        for path in candidates:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
        try:
            return ImageFont.truetype("Arial.ttf", size)
        except Exception:
            return ImageFont.load_default()

    num_font_candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Helvetica.ttc",
    ]
    name_font_candidates = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Helvetica.ttc",
    ]

    def ellipsize_text(draw_ctx, text, font_obj, max_w):
        if draw_ctx.textlength(text, font=font_obj) <= max_w:
            return text
        trimmed = text
        while trimmed and draw_ctx.textlength(trimmed + "...", font=font_obj) > max_w:
            trimmed = trimmed[:-1]
        return (trimmed + "...") if trimmed else "..."

    def wrap_text_lines(draw_ctx, text, font_obj, max_w, max_lines=3):
        words = text.split()
        if not words:
            return [""]

        lines = []
        idx = 0
        while idx < len(words) and len(lines) < max_lines:
            current = words[idx]
            idx += 1
            while idx < len(words):
                candidate = current + " " + words[idx]
                if draw_ctx.textlength(candidate, font=font_obj) <= max_w:
                    current = candidate
                    idx += 1
                else:
                    break
            lines.append(current)

        if idx < len(words) and lines:
            tail = " " + " ".join(words[idx:])
            lines[-1] = ellipsize_text(draw_ctx, lines[-1] + tail, font_obj, max_w)

        # Caso estremo: parola singola lunghissima
        for i, line in enumerate(lines):
            if draw_ctx.textlength(line, font=font_obj) > max_w:
                lines[i] = ellipsize_text(draw_ctx, line, font_obj, max_w)

        return lines

    def fit_num_font(draw_ctx, text, max_w):
        for size in (260, 240, 220, 200, 185, 170, 155, 140):
            try_font = load_font(num_font_candidates, size)
            bbox = draw_ctx.textbbox((0, 0), text, font=try_font)
            w = bbox[2] - bbox[0]
            if w <= max_w:
                return try_font
        return load_font(num_font_candidates, 130)

    pages = []
    for start in range(0, len(labels), per_page):
        chunk = labels[start:start + per_page]
        page = Image.new("RGB", (page_w, page_h), "white")
        draw = ImageDraw.Draw(page)

        # Griglia con bordi attaccati
        for c in range(cols + 1):
            x = x0 + c * cell_w
            draw.line([(x, y0), (x, y0 + grid_h)], fill="black", width=2)
        for r in range(rows + 1):
            y = y0 + r * cell_h
            draw.line([(x0, y), (x0 + grid_w, y)], fill="black", width=2)

        for i, (numero, nome) in enumerate(chunk):
            print(f"[PRINT] {numero}. {nome}")
            row = i // cols
            col = i % cols

            cx0 = x0 + col * cell_w
            cy0 = y0 + row * cell_h

            # Numero grande sopra
            num_font = fit_num_font(draw, numero, int(cell_w * 0.85))
            num_bbox = draw.textbbox((0, 0), numero, font=num_font)
            num_w = num_bbox[2] - num_bbox[0]
            num_x = cx0 + (cell_w - num_w) // 2
            num_y = cy0 + int(cell_h * 0.10)
            draw.text(
                (num_x, num_y),
                numero,
                fill="black",
                font=num_font,
                stroke_width=4,
                stroke_fill="black",
            )

            # Nome sotto (su una riga, centrato)
            clean_name = nome.strip()
            name_area_x = cx0 + 10
            name_area_w = max(40, cell_w - 20)
            name_area_top = cy0 + int(cell_h * 0.58)
            name_area_bottom = cy0 + cell_h - 10
            name_area_h = max(20, name_area_bottom - name_area_top)

            chosen_font = None
            chosen_lines = None
            chosen_line_h = None

            for font_size in (52, 48, 44, 40, 36, 32, 30, 28, 26, 24):
                try_font = load_font(name_font_candidates, font_size)
                try_lines = wrap_text_lines(draw, clean_name, try_font, name_area_w, max_lines=3)
                sample_bbox = draw.textbbox((0, 0), "Ag", font=try_font)
                line_h = (sample_bbox[3] - sample_bbox[1]) + int(font_size * 0.18)
                block_h = line_h * len(try_lines)
                if block_h <= name_area_h:
                    chosen_font = try_font
                    chosen_lines = try_lines
                    chosen_line_h = line_h
                    break

            if chosen_font is None:
                chosen_font = load_font(name_font_candidates, 22)
                chosen_lines = wrap_text_lines(draw, clean_name, chosen_font, name_area_w, max_lines=3)
                sample_bbox = draw.textbbox((0, 0), "Ag", font=chosen_font)
                chosen_line_h = (sample_bbox[3] - sample_bbox[1]) + 4

            block_h = chosen_line_h * len(chosen_lines)
            line_y = name_area_top + max(0, (name_area_h - block_h) // 2)
            for line in chosen_lines:
                line_bbox = draw.textbbox((0, 0), line, font=chosen_font)
                line_w = line_bbox[2] - line_bbox[0]
                line_x = cx0 + (cell_w - line_w) // 2
                draw.text((line_x, line_y), line, fill="black", font=chosen_font)
                line_y += chosen_line_h

        pages.append(page)

    output_pdf = os.path.join(script_dir, "etichette_set_a4.pdf")
    if len(pages) == 1:
        pages[0].save(output_pdf, "PDF", resolution=dpi)
    else:
        pages[0].save(
            output_pdf,
            "PDF",
            save_all=True,
            append_images=pages[1:],
            resolution=dpi,
        )

    dlg = tk.Toplevel(root)
    dlg.title("Stampa Set")
    dlg.transient(root)
    dlg.grab_set()
    dlg.resizable(False, False)

    tk.Label(
        dlg,
        text=(
            f"Creato PDF A4 in:\n{output_pdf}\n\n"
            "Stampa al 100% senza adattamento pagina."
        ),
        justify="left",
        anchor="w",
        padx=12,
        pady=10,
    ).pack(fill="both", expand=True)

    frame_btn = tk.Frame(dlg)
    frame_btn.pack(fill="x", padx=10, pady=(0, 10))

    def apri_file_pdf():
        try:
            subprocess.Popen(["open", output_pdf])
        except Exception:
            webbrowser.open_new_tab(output_pdf)

    tk.Button(frame_btn, text="OK", width=12, command=dlg.destroy).pack(side="right", padx=(6, 0))
    tk.Button(frame_btn, text="Apri file", width=12, command=apri_file_pdf).pack(side="right")

    dlg.update_idletasks()
    w = max(480, dlg.winfo_reqwidth())
    h = dlg.winfo_reqheight()
    x = root.winfo_rootx() + max(0, (root.winfo_width() - w) // 2)
    y = root.winfo_rooty() + max(0, (root.winfo_height() - h) // 2)
    dlg.geometry(f"{w}x{h}+{x}+{y}")
    dlg.wait_window()

# --- disabled sets ---
def load_disabled_sets():
    """Carica elenco set disabilitati da ui_settings.json."""
    global disabled_sets
    try:
        if os.path.exists(ui_settings_file):
            data = json.load(open(ui_settings_file))
            disabled_sets = set(data.get('disabled_sets', []))
    except Exception:
        disabled_sets = set()

def save_disabled_sets():
    """Salva elenco set disabilitati in ui_settings.json."""
    try:
        data = {}
        if os.path.exists(ui_settings_file):
            try:
                data = json.load(open(ui_settings_file))
            except Exception:
                data = {}
        data['disabled_sets'] = list(disabled_sets)
        with open(ui_settings_file, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f'[ERRORE] save_disabled_sets: {e}')

def load_lego_data():
    if os.path.exists(lego_data_file):
        try:
            return json.load(open(lego_data_file))
        except Exception:
            bak = lego_data_file + ".bak"
            if os.path.exists(bak):
                try:
                    print("[WARN] lego_data.json corrotto, carico backup .bak")
                    return json.load(open(bak))
                except Exception:
                    pass
            return {}
    return {}

def _write_lego_data(data):
    """Scrive lego_data.json su disco (thread separato) con backup .bak"""
    with save_lock:
        try:
            if os.path.exists(lego_data_file):
                shutil.copy2(lego_data_file, lego_data_file + ".bak")
            with open(lego_data_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[ERRORE] Salvataggio lego_data: {e}")

def save_lego_data(force=False):
    """Salva con batch system - evita I/O eccessivo ad ogni click"""
    global last_save_time, save_pending, save_thread
    now = time.time()

    if force or (now - last_save_time) >= SAVE_COOLDOWN:
        # Snapshot dati sul thread principale, scrittura asincrona
        data = {}
        for set_name, parts in sets.items():
            data[set_name] = {k: v["used"] for k, v in parts.items()}
        last_save_time = now
        save_pending = False
        save_thread = threading.Thread(target=_write_lego_data, args=(data,), daemon=True)
        save_thread.start()
    else:
        save_pending = True

# ------------------------
# AUTOCOMPLETE
# ------------------------
def on_type(event=None):
    codice = entry_set.get().strip() + "-1"
    if not codice:
        label_info.config(text="")
        return
    threading.Thread(target=check_set, args=(codice,)).start()

def check_set(codice):
    name, ok = get_set_info(codice)
    codes = load_mysets()
    if ok:
        stato = "🟢 già presente" if codice in codes else "⚪ nuovo"
        text = f"{name} ({stato})"
        color = "green"
    else:
        text = "❌ set non valido"
        color = "red"
    root.after(0, lambda: label_info.config(text=text, fg=color))

# ------------------------
# AGGIUNGI SET
# ------------------------
def aggiungi_set():
    codice = entry_set.get().strip() + "-1"
    if not codice:
        return
    codes = load_mysets()
    if codice in codes:
        risultato.config(text="Già presente", fg="orange")
        return
    name, ok = get_set_info(codice)
    if not ok:
        risultato.config(text="Set non valido", fg="red")
        return
    codes.append(codice)
    save_mysets(codes)
    risultato.config(text=f"Aggiunto {codice}", fg="green")
    entry_set.delete(0, tk.END)
    label_info.config(text="")
    carica_mysets()
    aggiorna_numerazione_set()

def aggiorna_numerazione_set():
        global numerazione_set
        codes = load_mysets()
        # Crea una lista di nomi set ordinata secondo l'ordine dei codici in mysets.json
        nome_per_codice = {v: k for k, v in set_codes.items()}
        ordinati = []
        for codice in codes:
            nome = nome_per_codice.get(codice)
            if nome and nome in sets:
                ordinati.append(nome)
        # Aggiungi eventuali set non presenti in mysets.json in fondo
        for nome in sets:
            if nome not in ordinati:
                ordinati.append(nome)
        numerazione_set = {nome: i for i, nome in enumerate(ordinati, start=1)}
        aggiorna_filtri_set()
        # --- funzione per salvare lego_data su file ---

def salva_lego_data():
    with open("lego_data.json", "w", encoding="utf-8") as f:
        json.dump(lego_data_file, f, ensure_ascii=False, indent=2)

def aggiorna_lista():
    global frame_inner

    # 🔥 FIX: evita crash se finestra chiusa
    if frame_inner is None or not frame_inner.winfo_exists():
        return

    # pulizia
    for w in frame_inner.winfo_children():
        w.destroy()

    for nome, s in sets.items():
        tot = sum(v["total"] for v in s.values())
        use = sum(v["used"] for v in s.values())

        f = tk.Frame(frame_inner)
        f.pack(fill="x", pady=2, padx=2)

        codice = set_codes.get(nome, nome)
        numero = numerazione_set.get(nome, "?")

        testo_label = f"{numero}. {nome} - {codice} → {use}/{tot}"
        tk.Label(f, text=testo_label, anchor="w").pack(side="left", fill="x", expand=True)

        # ------------------------
        # ELIMINA
        # ------------------------
        def elimina(n=codice, nome_set=nome):
            if messagebox.askyesno("Conferma", f"Cancellare {n}?"):
                sets.pop(nome_set, None)
                elimina_set_da_file(n)
                aggiorna_numerazione_set()
                aggiorna_lista()
                aggiorna_riepilogo()

        # ------------------------
        # AZZERA
        # ------------------------
        def azzera_pezzi(nome_set=nome):
            if messagebox.askyesno("Conferma", f"Azzera i pezzi usati per {nome_set}?"):

                # reset in memoria
                for pezzo in sets[nome_set].values():
                    pezzo["used"] = 0

                # reset su file (se presente)
                saved = load_lego_data()
                if nome_set in saved:
                    for k in saved[nome_set]:
                        saved[nome_set][k] = 0

                    with open(lego_data_file, "w") as f:
                        json.dump(saved, f, indent=2)

                aggiorna_global()
                aggiorna_lista()
                aggiorna_riepilogo()
                aggiorna_filtri_set()
                aggiorna_griglia(force=True)
                save_lego_data()

        tk.Button(f, text="Elimina", command=elimina).pack(side="right")
        tk.Button(f, text="Azzera", command=azzera_pezzi).pack(side="right", padx=5)
# ------------------------
# IMAGE
# ------------------------
def get_image_path(key):
    return os.path.join(image_dir, key.replace("/", "_") + ".png")

def download_image(key):
    path = get_image_path(key)
    if os.path.exists(path):
        return
    try:
        r = requests.get(global_parts[key]["img"], timeout=5)
        img = Image.open(BytesIO(r.content))
        img.save(path)
        print(f"[INFO] Immagine scaricata: {key}")
    except:
        pass

def download_image_from_url(key, url):
    """Scarica l'immagine di un pezzo usando l'URL fornito direttamente (per set non in global_parts)."""
    if not url:
        return
    path = get_image_path(key)
    if os.path.exists(path):
        return
    try:
        r = requests.get(url, timeout=7)
        img = Image.open(BytesIO(r.content))
        img.save(path)
        print(f"[INFO] Immagine scaricata (url): {key}")
    except:
        pass

def load_image_pair_async(key, size, lbl_widget, url_fallback=None):
    """Carica immagine in background e aggiorna lbl_widget quando pronta."""
    def _worker():
        # scarica su disco se non presente
        if not os.path.exists(get_image_path(key)):
            if key in global_parts and global_parts[key].get("img"):
                download_image(key)
            elif url_fallback:
                download_image_from_url(key, url_fallback)
        img_tk, _ = load_image_pair(key, size=size)
        if img_tk is None:
            return
        def _update():
            try:
                if lbl_widget.winfo_exists():
                    lbl_widget.config(image=img_tk, text="")
                    lbl_widget.image = img_tk
            except Exception:
                pass
        root.after(0, _update)
    threading.Thread(target=_worker, daemon=True).start()

def preload_images(keys):
    # Pillow non è thread-safe su Apple Silicon: loop sequenziale puro
    for k in keys:
        download_image(k)


def get_set_thumbnail_path(codice):
    safe = codice.replace("/", "_")
    return os.path.join(set_thumb_dir, f"{safe}.png")


def _set_label_image(lbl_widget, photo):
    try:
        if lbl_widget.winfo_exists():
            lbl_widget.config(image=photo)
            lbl_widget.image = photo
    except Exception:
        pass

def load_set_thumbnail(codice, lbl_widget, size=None):
    """Carica la miniatura del set in background e aggiorna lbl_widget quando pronta."""
    if size is None:
        size = SET_THUMB_SIZE

    if codice in set_photo_cache:
        try:
            if lbl_widget.winfo_exists():
                lbl_widget.config(image=set_photo_cache[codice])
                lbl_widget.image = set_photo_cache[codice]
        except Exception:
            pass
        return

    thumb_path = get_set_thumbnail_path(codice)

    def _worker():
        try:
            # 1) Cache su disco: evita download ad ogni avvio (caricata in background)
            if os.path.exists(thumb_path):
                try:
                    img_local = Image.open(thumb_path).convert("RGBA")
                    img_local.thumbnail(size, Image.LANCZOS)

                    def _update_local():
                        photo = ImageTk.PhotoImage(img_local)
                        set_photo_cache[codice] = photo
                        _set_label_image(lbl_widget, photo)
                    root.after(0, _update_local)
                    return
                except Exception:
                    pass

            url = set_img_url_mem.get(codice)
            if not url:
                try:
                    r = requests.get(
                        f"https://rebrickable.com/api/v3/lego/sets/{codice}/",
                        headers={"Authorization": f"key {API_KEY}"},
                        timeout=5
                    )
                    if r.status_code == 200:
                        url = r.json().get("set_img_url", "")
                        if url:
                            set_img_url_mem[codice] = url
                except Exception:
                    pass

            if not url:
                return

            try:
                r = requests.get(url, timeout=8)
                img_orig = Image.open(BytesIO(r.content)).convert("RGBA")

                # Salva la versione originale su disco per i prossimi avvii.
                try:
                    img_orig.save(thumb_path, format="PNG")
                except Exception:
                    pass

                img = img_orig.copy()
                img.thumbnail(size, Image.LANCZOS)

                def _update():
                    photo = ImageTk.PhotoImage(img)
                    set_photo_cache[codice] = photo
                    _set_label_image(lbl_widget, photo)
                root.after(0, _update)
            except Exception:
                pass
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()

def apri_anteprima_set_grande(codice_set, nome_set=None):
    """Apre una finestra con l'immagine grande del set selezionato."""
    global anteprima_set_last_position, anteprima_set_win, anteprima_set_ctx, anteprima_set_load_token

    if not codice_set:
        return

    titolo = f"Anteprima set: {codice_set}"
    if nome_set:
        titolo = f"Anteprima set: {codice_set} - {nome_set}"

    def _build_set_preview_details(codice, nome_hint=None):
        resolved_name = None

        if nome_hint and nome_hint in sets:
            resolved_name = nome_hint
        else:
            for n, c in set_codes.items():
                if c == codice:
                    resolved_name = n
                    break

        if resolved_name and resolved_name in sets:
            numero = numerazione_set.get(resolved_name, "?")
            set_data = sets[resolved_name]
            used = sum(v.get("used", 0) for v in set_data.values())
            total = sum(v.get("total", 0) for v in set_data.values())
            percent = int((used / total) * 100) if total > 0 else 0
            main_info = f"{numero}. {resolved_name}"
            sub_info = (
                f"Codice: {codice}   "
                f"Pezzi: {used}/{total} ({percent}%)   "
                f"Tipi pezzo: {len(set_data)}"
            )
            return main_info, sub_info

        if nome_hint:
            return nome_hint, f"Codice: {codice}"
        return "Set selezionato", f"Codice: {codice}"

    def _create_preview_window():
        nonlocal titolo
        global anteprima_set_win, anteprima_set_ctx

        win = tk.Toplevel(root)
        win.title(titolo)
        win.resizable(True, True)
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass

        frame_info = tk.Frame(win)
        frame_info.pack(fill="x", padx=10, pady=(10, 6))

        frame_info_text = tk.Frame(frame_info)
        frame_info_text.pack(side="left", fill="x", expand=True)

        lbl_info_main = tk.Label(
            frame_info_text,
            text="",
            anchor="w",
            font=("Arial", 12, "bold")
        )
        lbl_info_main.pack(fill="x")

        lbl_info_sub = tk.Label(
            frame_info_text,
            text="",
            anchor="w",
            fg="#444444"
        )
        lbl_info_sub.pack(fill="x")

        lbl_loading = tk.Label(
            frame_info,
            text="",
            anchor="e",
            width=14,
            fg="#1f4f99"
        )
        lbl_loading.pack(side="right", padx=(0, 8))

        lbl_img = tk.Label(win, text="Carico immagine...", padx=12, pady=12)
        lbl_img.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        image_state = {
            "orig": None,
            "resize_job": None,
            "transition_job": None,
            "current_render": None
        }
        spinner_state = {
            "job": None,
            "idx": 0,
            "active": False
        }

        def _remember_window_position():
            global anteprima_set_last_position
            try:
                if win.winfo_exists():
                    anteprima_set_last_position = (win.winfo_x(), win.winfo_y())
            except Exception:
                pass

        def _apply_window_geometry(width, height):
            global anteprima_set_last_position

            if anteprima_set_last_position is None:
                try:
                    root.update_idletasks()
                    x = root.winfo_rootx() + 70
                    y = root.winfo_rooty() + 70
                except Exception:
                    x = max(20, int((win.winfo_screenwidth() - width) / 2))
                    y = max(20, int((win.winfo_screenheight() - height) / 2))
            else:
                x, y = anteprima_set_last_position

            max_x = max(0, win.winfo_screenwidth() - width)
            max_y = max(0, win.winfo_screenheight() - height)
            x = max(0, min(x, max_x))
            y = max(0, min(y, max_y))
            win.geometry(f"{width}x{height}+{x}+{y}")

        def _render_scaled_image():
            if image_state["orig"] is None:
                return
            try:
                win.update_idletasks()
                available_w = max(220, win.winfo_width() - 40)
                available_h = max(180, win.winfo_height() - frame_info.winfo_height() - 50)

                img = image_state["orig"].copy()
                img.thumbnail((available_w, available_h), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                lbl_img.config(image=photo, text="")
                lbl_img.image = photo
                image_state["current_render"] = img
            except Exception:
                lbl_img.config(text="Immagine non disponibile", image="")
                lbl_img.image = None

        def _cancel_transition():
            if image_state["transition_job"] is not None:
                try:
                    win.after_cancel(image_state["transition_job"])
                except Exception:
                    pass
                image_state["transition_job"] = None

        def _fit_for_view(img_orig):
            win.update_idletasks()
            available_w = max(220, win.winfo_width() - 40)
            available_h = max(180, win.winfo_height() - frame_info.winfo_height() - 50)
            img = img_orig.copy().convert("RGBA")
            img.thumbnail((available_w, available_h), Image.Resampling.LANCZOS)
            return img

        def _render_photo(img_pil):
            photo = ImageTk.PhotoImage(img_pil)
            lbl_img.config(image=photo, text="")
            lbl_img.image = photo

        def _crossfade_to_new_image(new_orig):
            _cancel_transition()

            try:
                new_img = _fit_for_view(new_orig)
            except Exception:
                new_img = None

            if new_img is None:
                _render_scaled_image()
                return

            old_img = image_state.get("current_render")
            if old_img is None:
                image_state["current_render"] = new_img
                _render_photo(new_img)
                return

            try:
                w = max(old_img.size[0], new_img.size[0])
                h = max(old_img.size[1], new_img.size[1])
                base_old = Image.new("RGBA", (w, h), (255, 255, 255, 0))
                base_new = Image.new("RGBA", (w, h), (255, 255, 255, 0))

                ox = (w - old_img.size[0]) // 2
                oy = (h - old_img.size[1]) // 2
                nx = (w - new_img.size[0]) // 2
                ny = (h - new_img.size[1]) // 2
                base_old.paste(old_img, (ox, oy), old_img)
                base_new.paste(new_img, (nx, ny), new_img)

                steps = 7
                interval_ms = 22

                def _step(i=1):
                    alpha = min(1.0, i / float(steps))
                    frame = Image.blend(base_old, base_new, alpha)
                    _render_photo(frame)
                    if i < steps:
                        image_state["transition_job"] = win.after(interval_ms, lambda: _step(i + 1))
                    else:
                        image_state["transition_job"] = None
                        image_state["current_render"] = new_img

                _step(1)
            except Exception:
                image_state["current_render"] = new_img
                _render_photo(new_img)

        def _schedule_render(_event=None):
            _cancel_transition()
            if image_state["resize_job"] is not None:
                try:
                    win.after_cancel(image_state["resize_job"])
                except Exception:
                    pass
            image_state["resize_job"] = win.after(40, _render_scaled_image)
            _remember_window_position()

        def _close_preview_window():
            global anteprima_set_win, anteprima_set_ctx
            spinner_state["active"] = False
            _cancel_transition()
            if spinner_state["job"] is not None:
                try:
                    win.after_cancel(spinner_state["job"])
                except Exception:
                    pass
                spinner_state["job"] = None
            _remember_window_position()
            if win.winfo_exists():
                win.destroy()
            anteprima_set_win = None
            anteprima_set_ctx = None

        def _spinner_tick():
            if not spinner_state["active"]:
                return
            frames = ["loading.", "loading..", "loading...", "loading...."]
            lbl_loading.config(text=frames[spinner_state["idx"] % len(frames)])
            spinner_state["idx"] += 1
            spinner_state["job"] = win.after(130, _spinner_tick)

        def _start_spinner():
            if spinner_state["active"]:
                return
            spinner_state["active"] = True
            spinner_state["idx"] = 0
            _spinner_tick()

        def _stop_spinner():
            spinner_state["active"] = False
            if spinner_state["job"] is not None:
                try:
                    win.after_cancel(spinner_state["job"])
                except Exception:
                    pass
                spinner_state["job"] = None
            lbl_loading.config(text="")

        tk.Button(frame_info, text="Chiudi", command=_close_preview_window).pack(side="right")

        win.bind("<Configure>", _schedule_render)
        win.protocol("WM_DELETE_WINDOW", _close_preview_window)
        win.minsize(420, 280)
        _apply_window_geometry(900, 650)

        anteprima_set_win = win
        anteprima_set_ctx = {
            "win": win,
            "lbl_info_main": lbl_info_main,
            "lbl_info_sub": lbl_info_sub,
            "lbl_loading": lbl_loading,
            "lbl_img": lbl_img,
            "image_state": image_state,
            "schedule_render": _schedule_render,
            "crossfade_to_new_image": _crossfade_to_new_image,
            "apply_window_geometry": _apply_window_geometry,
            "start_spinner": _start_spinner,
            "stop_spinner": _stop_spinner
        }

    created_now = False
    if anteprima_set_win is None or not anteprima_set_win.winfo_exists() or anteprima_set_ctx is None:
        _create_preview_window()
        created_now = True

    win = anteprima_set_ctx["win"]
    lbl_info_main = anteprima_set_ctx["lbl_info_main"]
    lbl_info_sub = anteprima_set_ctx["lbl_info_sub"]
    lbl_img = anteprima_set_ctx["lbl_img"]
    image_state = anteprima_set_ctx["image_state"]
    schedule_render = anteprima_set_ctx["schedule_render"]
    crossfade_to_new_image = anteprima_set_ctx["crossfade_to_new_image"]
    start_spinner = anteprima_set_ctx["start_spinner"]
    stop_spinner = anteprima_set_ctx["stop_spinner"]
    info_main, info_sub = _build_set_preview_details(codice_set, nome_set)

    win.title(titolo)
    lbl_info_main.config(text=info_main)
    lbl_info_sub.config(text=info_sub)
    start_spinner()
    if image_state["orig"] is None and not lbl_img.cget("image"):
        lbl_img.config(text="Carico immagine...")
    # Evita blink della finestra su macOS: non forzare focus a ogni cambio set.
    if created_now:
        win.lift()
        win.focus_force()

    anteprima_set_load_token += 1
    request_token = anteprima_set_load_token

    def _show_image(img_orig, token):
        global anteprima_set_load_token
        if token != anteprima_set_load_token:
            return
        try:
            image_state["orig"] = img_orig
            crossfade_to_new_image(img_orig)
            stop_spinner()
        except Exception:
            stop_spinner()
            lbl_img.config(text="Immagine non disponibile")

    def _show_error(msg, token):
        global anteprima_set_load_token
        if token != anteprima_set_load_token:
            return
        stop_spinner()
        lbl_img.config(text=msg, image="")
        lbl_img.image = None
        image_state["orig"] = None

    def _worker():
        try:
            img_orig = None
            img_path = get_set_thumbnail_path(codice_set)

            if os.path.exists(img_path):
                try:
                    img_orig = Image.open(img_path).convert("RGBA")
                except Exception:
                    img_orig = None

            if img_orig is None:
                url = set_img_url_mem.get(codice_set)
                if not url:
                    try:
                        r = requests.get(
                            f"https://rebrickable.com/api/v3/lego/sets/{codice_set}/",
                            headers={"Authorization": f"key {API_KEY}"},
                            timeout=5
                        )
                        if r.status_code == 200:
                            url = r.json().get("set_img_url", "")
                            if url:
                                set_img_url_mem[codice_set] = url
                    except Exception:
                        url = ""

                if url:
                    try:
                        r = requests.get(url, timeout=8)
                        img_orig = Image.open(BytesIO(r.content)).convert("RGBA")
                        try:
                            img_orig.save(img_path, format="PNG")
                        except Exception:
                            pass
                    except Exception:
                        img_orig = None

            if img_orig is None:
                root.after(0, lambda t=request_token: _show_error("Immagine set non disponibile", t))
                return

            root.after(0, lambda img=img_orig, t=request_token: _show_image(img, t))
        except Exception:
            root.after(0, lambda t=request_token: _show_error("Errore caricamento immagine", t))

    threading.Thread(target=_worker, daemon=True).start()

from PIL import ImageEnhance

def load_image_pair(key, size=None):
    if size is None:
        size = ICON_SIZE
    cache_key = f"{key}_{size}"

    if cache_key in image_cache:
        # LRU: sposta in fondo (elemento più recente)
        image_cache.move_to_end(cache_key)
        return image_cache[cache_key]

    path = get_image_path(key)

    try:
        if os.path.exists(path):
            img = Image.open(path)
            img = img.resize((size, size), Image.Resampling.LANCZOS)

            img_tk = ImageTk.PhotoImage(img)

            # 🔥 versione scura
            enhancer = ImageEnhance.Brightness(img)
            img_dark = enhancer.enhance(0.4)
            img_dark_tk = ImageTk.PhotoImage(img_dark)

            image_cache[cache_key] = (img_tk, img_dark_tk)
            # LRU: evict il più vecchio se oltre il limite
            if len(image_cache) > MAX_IMAGE_CACHE_SIZE:
                image_cache.popitem(last=False)
            return image_cache[cache_key]
    except:
        pass

    return None, None

def get_used_from_file(key):
    """Calcola il totale usato per un pezzo leggendo dal file lego_data.json"""
    try:
        data = load_lego_data()
        total_used = 0
        for set_name, parts in data.items():
            if key in parts:
                total_used += parts[key]
        return total_used
    except:
        return 0

# ------------------------
# LOGICA
# ------------------------
def aggiorna_global():
    global global_parts
    global_parts = {}
    for nome_s, s in sets.items():
        if nome_s in disabled_sets:
            continue
        for k, v in s.items():
            if k not in global_parts:
                global_parts[k] = v.copy()
            else:
                global_parts[k]["total"] += v["total"]
                global_parts[k]["used"] += v["used"]

def lunghezza_pezzo_valore(nome, part_num=None):
    """Restituisce la lunghezza massima in stud.
    Prima cerca nel DB BrickLink (stud_dim reale), poi fa regex sul nome."""
    # 0) DB BrickLink (dati reali)
    if part_num and part_num in piece_dimensions_db:
        entry = piece_dimensions_db[part_num]
        # usa le dimensioni in stud se disponibili
        if "stud_x" in entry and "stud_y" in entry:
            return max(entry["stud_x"], entry["stud_y"])

    if not nome:
        return 0

    testo = nome.lower()

    # 1) Priorita ai pattern dimensionali reali (es: "1 x 3", "2x2", "1x2x5")
    dims = re.findall(r'(\d+)\s*[x]\s*(\d+)(?:\s*[x]\s*(\d+))?', testo)
    if dims:
        valori = []
        for trio in dims:
            for n in trio:
                if not n:
                    continue
                try:
                    v = int(n)
                except Exception:
                    continue
                if 0 < v <= 40:
                    valori.append(v)
        if valori:
            return max(valori)

    # 2) Fallback: numeri singoli, ma ignora gradi/mm e valori fuori scala
    candidati = []
    for m in re.finditer(r'(\d+)', testo):
        try:
            v = int(m.group(1))
        except Exception:
            continue

        after = testo[m.end():m.end() + 3]
        if '\u00b0' in after or 'mm' in after:
            continue

        if 0 < v <= 40:
            candidati.append(v)

    if candidati:
        return max(candidati)

    return 0

def lunghezza_pezzo(nome, part_num=None):
    valore = lunghezza_pezzo_valore(nome, part_num)
    # indica fonte dati
    if part_num and part_num in piece_dimensions_db:
        entry = piece_dimensions_db[part_num]
        if "stud_x" in entry:
            return f"{valore} stud ✓"
    return f"{valore} stud" if valore > 0 else ""

def get_piece_dimension_data(part_num):
    if not part_num:
        return {}
    return piece_dimensions_db.get(part_num, {})

def get_pack_max_cm(part_num):
    entry = get_piece_dimension_data(part_num)
    values = []
    for key in ("pack_x", "pack_y", "pack_z"):
        value = entry.get(key)
        if isinstance(value, (int, float)):
            values.append(float(value))
    if not values:
        return None
    return max(values)

def get_weight_g(part_num):
    entry = get_piece_dimension_data(part_num)
    value = entry.get("weight_g")
    if isinstance(value, (int, float)):
        return float(value)
    return None

def passa_filtri_dimensionali(part_num):
    weight_mode = weight_filter_var.get() if weight_filter_var is not None else "Tutti"
    pack_mode = pack_filter_var.get() if pack_filter_var is not None else "Tutti"
    entry = get_piece_dimension_data(part_num)

    weight = entry.get("weight_g")
    if weight_mode != "Tutti":
        if not isinstance(weight, (int, float)):
            return False
        weight = float(weight)
        if weight_mode == "<= 0.5g" and weight > 0.5:
            return False
        if weight_mode == "0.5g - 2g" and (weight < 0.5 or weight > 2.0):
            return False
        if weight_mode == ">= 2g" and weight < 2.0:
            return False

    pack_max = get_pack_max_cm(part_num)
    if pack_mode != "Tutti":
        if pack_max is None:
            return False
        if pack_mode == "<= 2cm" and pack_max > 2.0:
            return False
        if pack_mode == "2cm - 4cm" and (pack_max < 2.0 or pack_max > 4.0):
            return False
        if pack_mode == ">= 4cm" and pack_max < 4.0:
            return False

    return True

# Mappa nomi colore Rebrickable → (bg hex, fg hex per contrasto)
LEGO_COLOR_HEX = {
    "Black": ("#1B2A34", "#ffffff"),
    "Blue": ("#0055BF", "#ffffff"),
    "Bright Pink": ("#FF94C2", "#000000"),
    "Brown": ("#582A12", "#ffffff"),
    "Coral": ("#FF698F", "#000000"),
    "Dark Azure": ("#078BC9", "#ffffff"),
    "Dark Blue": ("#0A3463", "#ffffff"),
    "Dark Brown": ("#352100", "#ffffff"),
    "Dark Green": ("#184632", "#ffffff"),
    "Dark Orange": ("#A95500", "#ffffff"),
    "Dark Purple": ("#3F3691", "#ffffff"),
    "Dark Red": ("#720E0F", "#ffffff"),
    "Dark Tan": ("#958A73", "#000000"),
    "Dark Turquoise": ("#008F9B", "#ffffff"),
    "Flat Dark Gold": ("#B4883E", "#000000"),
    "Flat Silver": ("#898788", "#ffffff"),
    "Green": ("#237841", "#ffffff"),
    "Lavender": ("#E1D5ED", "#000000"),
    "Light Aqua": ("#ADC3C0", "#000000"),
    "Light Blue": ("#9FC3E9", "#000000"),
    "Light Bluish Gray": ("#AFB5C7", "#000000"),
    "Light Nougat": ("#F6A86B", "#000000"),
    "Light Purple": ("#CD6298", "#ffffff"),
    "Light Yellow": ("#FFE001", "#000000"),
    "Lime": ("#BBE90B", "#000000"),
    "Magenta": ("#923978", "#ffffff"),
    "Medium Azure": ("#36AEBE", "#000000"),
    "Medium Blue": ("#5A93DB", "#ffffff"),
    "Medium Dark Flesh": ("#CC702A", "#000000"),
    "Medium Lavender": ("#AC78BA", "#ffffff"),
    "Medium Nougat": ("#AA7D55", "#ffffff"),
    "Medium Orange": ("#FFA70B", "#000000"),
    "Metallic Silver": ("#A5A9B4", "#000000"),
    "Nougat": ("#D09168", "#000000"),
    "Olive Green": ("#9B9A5A", "#000000"),
    "Orange": ("#FE8A18", "#000000"),
    "Pearl Dark Gray": ("#575857", "#ffffff"),
    "Pearl Gold": ("#CC9C2B", "#000000"),
    "Pearl Light Gold": ("#DCBE61", "#000000"),
    "Pearl Very Light Gray": ("#E6E3E0", "#000000"),
    "Pink": ("#FC97AC", "#000000"),
    "Purple": ("#81007B", "#ffffff"),
    "Red": ("#C91A09", "#ffffff"),
    "Reddish Brown": ("#82422A", "#ffffff"),
    "Sand Blue": ("#6074A1", "#ffffff"),
    "Sand Green": ("#789F8A", "#000000"),
    "Sand Purple": ("#845E84", "#ffffff"),
    "Sand Yellow": ("#D7C599", "#000000"),
    "Tan": ("#E4CD9E", "#000000"),
    "Trans-Black": ("#635F52", "#ffffff"),
    "Trans-Blue": ("#0020A0", "#ffffff"),
    "Trans-Clear": ("#FCFCFC", "#000000"),
    "Trans-Dark Blue": ("#0020A0", "#ffffff"),
    "Trans-Dark Pink": ("#DF6695", "#ffffff"),
    "Trans-Green": ("#84B68D", "#000000"),
    "Trans-Light Blue": ("#68BCC5", "#000000"),
    "Trans-Medium Blue": ("#559AB7", "#ffffff"),
    "Trans-Neon Green": ("#F8F184", "#000000"),
    "Trans-Neon Orange": ("#FF800D", "#000000"),
    "Trans-Neon Yellow": ("#DAB000", "#000000"),
    "Trans-Orange": ("#F08F1C", "#000000"),
    "Trans-Purple": ("#C870A0", "#000000"),
    "Trans-Red": ("#C91A09", "#ffffff"),
    "Trans-Yellow": ("#F5CD2F", "#000000"),
    "Very Light Bluish Gray": ("#E3E6EA", "#000000"),
    "Very Light Gray": ("#E6E3DA", "#000000"),
    "White": ("#FFFFFF", "#000000"),
    "Yellow": ("#F2CD37", "#000000"),
    "Yellowish Green": ("#DFEEA5", "#000000"),
}

PIECE_TYPE_OPTIONS = [
    "Tutti",
    "Brick",
    "Plate",
    "Tile",
    "Slope",
    "Technic",
    "Wheel",
    "Panel",
    "Wedge",
    "Hinge",
    "Minifig",
    "Altro"
]

def get_piece_type(name, part_num=None):
    text = f"{part_num or ''} {name or ''}".lower()
    if "minifig" in text or "minifigure" in text:
        return "Minifig"
    if any(token in text for token in ["technic", "liftarm", "beam", "connector", "axle", "pin", "bush", "gear"]):
        return "Technic"
    if any(token in text for token in ["wheel", "tyre", "tire", "rim"]):
        return "Wheel"
    if "hinge" in text:
        return "Hinge"
    if "panel" in text:
        return "Panel"
    if "wedge" in text:
        return "Wedge"
    if "slope" in text:
        return "Slope"
    if "tile" in text:
        return "Tile"
    if "plate" in text:
        return "Plate"
    if "brick" in text:
        return "Brick"
    return "Altro"

def build_piece_info_text(k, v, used, total):
    part_num = k.rsplit("_", 1)[0]
    entry = get_piece_dimension_data(part_num)

    stud_txt = lunghezza_pezzo(v.get("name", ""), part_num)
    if not stud_txt:
        stud_txt = "-"

    if isinstance(entry.get("weight_g"), (int, float)):
        peso_txt = f"{float(entry['weight_g']):.2f}g"
    else:
        peso_txt = "-"

    pack_txt = entry.get("pack_dim", "-")

    return (
        f"{used}/{total}\n"
        f"Stud: {stud_txt}\n"
        f"Peso: {peso_txt}\n"
        f"Pack: {pack_txt}"
    )


def _matches_selected_color(piece_key, selected_color):
    if selected_color == "Tutti":
        return True

    colore = piece_key.split("_")[1]
    if selected_color == "Trans":
        return is_trans_color(colore)
    return colore == selected_color

def format_set_filter_label(nome_set, selected_color=None):
    if selected_color is None:
        selected_color = color_var.get() if color_var is not None else "Tutti"

    numero = numerazione_set.get(nome_set)
    s = sets.get(nome_set, {})
    if s:
        if selected_color != "Tutti":
            usati_colore = 0
            mancanti_colore = 0
            totali_colore = 0
            for key, value in s.items():
                if not _matches_selected_color(key, selected_color):
                    continue
                used = value.get("used", 0)
                total = value.get("total", 0)
                usati_colore += used
                mancanti_colore += max(total - used, 0)
                totali_colore += total
            has_missing = mancanti_colore > 0
            indicator = "🔴 " if has_missing else "🟢 "
            info = f" {indicator}[{usati_colore}/{totali_colore}]"
        else:
            usati = sum(v["used"] for v in s.values())
            totali = sum(v["total"] for v in s.values())
            mancanti = totali - usati
            has_missing = mancanti > 0
            indicator = "🔴 " if has_missing else "🟢 "
            info = f" {indicator}[{usati}/{totali}]"
    else:
        info = ""
    if numero is None:
        return f"{nome_set}{info}{SET_LABEL_SUFFIX}"
    return f"{numero}. {nome_set}{info}{SET_LABEL_SUFFIX}"

def get_selected_set_name():
    selected_label = set_var.get() if set_var is not None else get_all_sets_label()
    selected_name = set_filter_map.get(selected_label)
    if selected_name:
        return selected_name

    # Fallback robusto: la label include contatori dinamici [used/total]
    # che possono cambiare e non matchare piu la chiave in set_filter_map.
    if not selected_label or selected_label == "Tutti":
        return None

    normalized = selected_label.strip()
    normalized = re.sub(r"^\d+\.\s*", "", normalized)
    # Rimuove indicatore emoji (🔴/🟢) + contatori [x/y] dalla fine della label
    normalized = re.sub(r"\s*[🔴🟢]?\s*\[[^\]]+\]\s*$", "", normalized).strip()

    if normalized in sets:
        return normalized

    return None

def get_source_parts():
    selected_set = get_selected_set_name()
    if selected_set and selected_set in sets:
        return sets[selected_set], selected_set
    return global_parts, None

def get_piece_counts(key, source_set=None):
    if source_set and source_set in sets and key in sets[source_set]:
        dati = sets[source_set][key]
        return dati.get("used", 0), dati.get("total", 0)

    dati = global_parts.get(key, {})
    return dati.get("used", get_used_from_file(key)), dati.get("total", 0)

def _ensure_piece_set_cache_valid():
    global piece_cache_signature, piece_set_names_cache
    signature = tuple(sorted(sets.keys()))
    if signature != piece_cache_signature:
        piece_set_names_cache.clear()
        piece_cache_signature = signature

def get_available_set_names_for_piece(key):
    selected_set = get_selected_set_name()
    if selected_set and selected_set in sets:
        if key in sets[selected_set]:
            return [selected_set]
        return []
    _ensure_piece_set_cache_valid()
    if key not in piece_set_names_cache:
        disponibili = [nome for nome, s in sets.items() if key in s]
        piece_set_names_cache[key] = sorted(disponibili, key=lambda nome: numerazione_set.get(nome, 9999))
    return piece_set_names_cache.get(key, [])

def get_all_set_names_for_piece(key):
    """Ritorna sempre tutti i set che contengono il pezzo, ignorando il filtro set."""
    _ensure_piece_set_cache_valid()
    if key not in piece_set_names_cache:
        disponibili = [nome for nome, s in sets.items() if key in s]
        piece_set_names_cache[key] = sorted(disponibili, key=lambda nome: numerazione_set.get(nome, 9999))
    return piece_set_names_cache.get(key, [])

def get_next_destinazione_pezzo(key):
    """Restituisce il prossimo set reale che verra usato da click_pezzo per questo pezzo."""
    for nome in get_available_set_names_for_piece(key):
        s = sets.get(nome, {})
        if key in s and s[key]["used"] < s[key]["total"]:
            used = s[key]["used"]
            total = s[key]["total"]
            rem = total - used
            numero = numerazione_set.get(nome, "?")
            return nome, numero, rem, used, total
    return None

def aggiorna_filtri_set():
    global set_filter_map, set_menu_dynamic_width, prev_set_labels_cache
    if set_var is None or menu_set is None:
        return

    selected_name = get_selected_set_name()
    selected_color = color_var.get() if color_var is not None else "Tutti"
    set_filter_map = {"Tutti": None}

    ordinati = sorted(sets.keys(), key=lambda nome: numerazione_set.get(nome, 9999))
    labels = ["Tutti"]
    for nome in ordinati:
        if nome in disabled_sets:
            continue
        label = format_set_filter_label(nome, selected_color)
        labels.append(label)
        set_filter_map[label] = nome

    # 🔥 CACHE: confronta solo i nomi set (non le label che includono contatori dinamici)
    # Rigenera il menu solo se i set cambiano, non ad ogni aggiornamento contatori
    set_names_key = ["Tutti", f"color:{selected_color}"] + [nome for nome in ordinati if nome not in disabled_sets]
    if set_names_key == prev_set_labels_cache and selected_color == "Tutti":
        # Set invariati: aggiorna solo la label della selezione corrente (contatori freschi)
        if selected_name and selected_name in sets:
            set_var.set(format_set_filter_label(selected_name, selected_color))
        aggiorna_progresso_set()
        if color_var is not None:
            aggiorna_colori()
        return

    prev_set_labels_cache = set_names_key.copy()

    menu_set['menu'].delete(0, 'end')
    for label in labels:
        menu_set['menu'].add_command(
            label=label,
            command=lambda value=label: on_set_filter_change(value)
        )

    # Auto-width in base alla label piu lunga del menu set (fisso: lunghezza testo + 2)
    longest = max((len(label) for label in labels), default=8)
    set_menu_dynamic_width = longest
    apply_set_menu_width()

    if selected_name and selected_name in sets:
        set_var.set(format_set_filter_label(selected_name, selected_color))
    else:
        set_var.set("Tutti")

    aggiorna_progresso_set()
    if color_var is not None:
        aggiorna_colori()

# 🔥 Cache e Debounce per evitare reload frequenti durante hover su dropdown
filter_debounce_job = None
prev_filter_state = {"color": None, "set": None, "sort": None}

def _execute_filter_update():
    global filter_debounce_job, prev_filter_state
    filter_debounce_job = None
    aggiorna_griglia()
    aggiorna_batch_da_filtri()
    aggiorna_progresso_set()
    # Salva lo stato dopo il render
    selected_color = color_var.get() if color_var is not None else "Tutti"
    selected_set = get_selected_set_name()
    current_sort = ordine_var.get() if ordine_var is not None else "Stud"
    solo_mancanti = bool(solo_mancanti_var.get()) if solo_mancanti_var is not None else False
    togli_completi = bool(togli_completi_var.get()) if togli_completi_var is not None else False
    togli_zero = bool(togli_zero_var.get()) if togli_zero_var is not None else False
    cerca_text = cerca_var.get().strip().lower() if cerca_var is not None else ""
    selected_piece_type = piece_type_var.get() if piece_type_var is not None else "Tutti"
    prev_filter_state = {"color": selected_color, "set": selected_set, "sort": current_sort, "solo_mancanti": solo_mancanti, "togli_completi": togli_completi, "togli_zero": togli_zero, "cerca": cerca_text, "piece_type": selected_piece_type}

def schedule_filter_update():
    """Schedula update griglia SOLO se filtri sono effettivamente cambiati"""
    global filter_debounce_job, prev_filter_state
    
    # 🔥 CHECK IMMEDIATO: se filtri non sono cambiati, niente da fare
    selected_color = color_var.get() if color_var is not None else "Tutti"
    selected_set = get_selected_set_name()
    current_sort = ordine_var.get() if ordine_var is not None else "Stud"
    solo_mancanti = bool(solo_mancanti_var.get()) if solo_mancanti_var is not None else False
    togli_completi = bool(togli_completi_var.get()) if togli_completi_var is not None else False
    togli_zero = bool(togli_zero_var.get()) if togli_zero_var is not None else False
    cerca_text = cerca_var.get().strip().lower() if cerca_var is not None else ""
    selected_piece_type = piece_type_var.get() if piece_type_var is not None else "Tutti"
    current_state = {"color": selected_color, "set": selected_set, "sort": current_sort, "solo_mancanti": solo_mancanti, "togli_completi": togli_completi, "togli_zero": togli_zero, "cerca": cerca_text, "piece_type": selected_piece_type}

    # Se stato è identico al precedente, abort - griglia resta statica
    if current_state == prev_filter_state:
        return
    
    # Filtri sono cambiati: schedula aggiornamento con debounce
    if filter_debounce_job is not None:
        try:
            root.after_cancel(filter_debounce_job)
        except Exception:
            pass
    # Ritarda aggiornamento: 150ms per dataset grandi (+ reattivo che 80ms ma cala lag)
    filter_debounce_job = root.after(150, _execute_filter_update)

def on_set_filter_change(label):
    set_var.set(label)
    aggiorna_colori()
    schedule_filter_update()

def on_color_filter_change(colore):
    color_var.set(colore)
    aggiorna_filtri_set()
    schedule_filter_update()

def is_trans_color(colore_nome):
    return isinstance(colore_nome, str) and colore_nome.lower().startswith("trans")

def on_piece_type_filter_change(value):
    if piece_type_var is not None:
        piece_type_var.set(value)
    schedule_filter_update()

def aggiorna_batch_da_filtri(reset_to_first=False):
    """Aggiorna batch window se aperta - chiamata da debounce filtri"""
    global batch_refresh_func, batch_win
    if batch_refresh_func and batch_win is not None and batch_win.winfo_exists():
        try:
            batch_refresh_func(reset_to_first=reset_to_first)
        except Exception:
            try:
                batch_refresh_func()
            except Exception:
                pass

# ------------------------
# IMPORT
# ------------------------
def carica_mysets():
    global last_selected
    codes = load_mysets()
    if sets:
        save_lego_data(force=True)
    last_selected = None
    threading.Thread(target=_import_fast, args=(codes,)).start()

def _import_fast(codes):
    # Aspetta che l'eventuale salvataggio asincrono sia completato prima di rileggere lego_data
    global save_thread
    if save_thread is not None and save_thread.is_alive():
        save_thread.join(timeout=5)

    offline_data = load_offline_sets()
    offline_set_codes = offline_data["set_codes"]
    offline_parts = offline_data["parts"]
    offline_set_images = offline_data.get("set_images", {})

    # Carica URL immagini dalla cache offline
    for cod, url in offline_set_images.items():
        if url:
            set_img_url_mem[cod] = url

    # 🚀 OTTIMIZZAZIONE: invertire dict per O(1) lookup invece di O(n)
    offline_codice_to_nome = {cod: nome for nome, cod in offline_set_codes.items()}

    root.after(0, lambda: risultato.config(text="Carico set...", fg="green"))

    tentativi_max = 5
    tentativo = 0
    mancanti = set(codes)
    while mancanti and tentativo < tentativi_max:
        scaricati_ora = set()
        for codice in list(mancanti):
            # 1️⃣ SE ESISTE OFFLINE → CARICALO (NON SKIPPARE)
            if codice in offline_codice_to_nome:
                nome = offline_codice_to_nome[codice]
                print(f"[OFFLINE] Carico set da file: {codice} -> {nome}")
                parts = offline_parts.get(nome, {})
                if parts:
                    sets[nome] = parts
                    set_codes[nome] = codice
                    scaricati_ora.add(codice)
                    continue

            # 2️⃣ SE GIÀ IN MEMORIA → SKIP
            if codice in set_codes.values():
                print(f"[SKIP] Già in memoria: {codice}")
                scaricati_ora.add(codice)
                continue

            # 3️⃣ SCARICA ONLINE
            name, ok = get_set_info(codice)
            if not ok:
                print(f"[ERRORE] Info set non trovate: {codice}")
                continue

            print(f"[DOWNLOAD] {codice} -> {name}")
            parts = scarica_set(codice)
            if parts:
                sets[name] = parts
                set_codes[name] = codice
                offline_set_codes[name] = codice
                offline_parts[name] = parts
                # Salva anche l'URL immagine nell'offline cache
                offline_set_images_updated = offline_data.get("set_images", {})
                if codice in set_img_url_mem:
                    offline_set_images_updated[codice] = set_img_url_mem[codice]
                save_offline_sets({
                    "set_codes": offline_set_codes,
                    "parts": offline_parts,
                    "set_images": offline_set_images_updated
                })
                scaricati_ora.add(codice)

        mancanti -= scaricati_ora
        tentativo += 1
        if mancanti:
            print(f"[RIPROVO] Mancano ancora {len(mancanti)} set. Tentativo {tentativo}/{tentativi_max}")
            time.sleep(2)

    if mancanti:
        print(f"[ATTENZIONE] Non sono stati caricati tutti i set dopo {tentativi_max} tentativi: {mancanti}")

    # 4️⃣ RIPRISTINA PEZZI USATI
    saved_data = load_lego_data()
    for nome in sets:
        if nome in saved_data:
            for k, u in saved_data[nome].items():
                if k in sets[nome]:
                    sets[nome][k]["used"] = int(u)

    # 5️⃣ AGGIORNA GLOBAL
    aggiorna_global()
    print(f"[INFO] global_parts: {len(global_parts)}")

    # 6️⃣ IMMAGINI
    root.after(0, lambda: risultato.config(text="Carico immagini...", fg="orange"))
    preload_images(global_parts.keys())

    # 7️⃣ UI UPDATE — un solo refresh sul thread Tk evita una griglia vuota
    # quando il download termina dopo il primo disegno della finestra.
    def finalizza_import_ui():
        aggiorna_numerazione_set()
        aggiorna_filtri_set()
        aggiorna_colori()
        applica_filtri_salvati()
        aggiorna_griglia(force=True)
        aggiorna_riepilogo()
        aggiorna_progresso_set()
        aggiorna_titolo()
        canvas.yview_moveto(0)
        root.update_idletasks()
        schedule_refresh_visible_grid(force=True)
        risultato.config(text="✅ COMPLETO", fg="green")

        def verifica_griglia_import():
            canvas.update_idletasks()
            print(
                f"[GRID] pezzi={len(grid_visible_items)} celle={len(ui_refs)} "
                f"canvas={canvas.winfo_width()}x{canvas.winfo_height()}"
            )
            if grid_visible_items and not ui_refs:
                aggiorna_griglia(force=True)
                canvas.yview_moveto(0)
                schedule_refresh_visible_grid(force=True)

        root.after(1000, verifica_griglia_import)

    # Non chiamare root.after dal thread di download: se termina prima che
    # mainloop sia attivo, Tk su macOS può perdere il callback.
    import_ui_requests.put(finalizza_import_ui)
    print("[FINE] Importazione completata")

# ------------------------
# GESTIONE SET
# ------------------------
def elimina_set_da_file(codice_set):
    if not os.path.exists(mysets_file):
        return
    with open(mysets_file, "r") as f:
        try:
            lista_set = json.load(f)
        except:
            lista_set = []
    if codice_set in lista_set:
        lista_set.remove(codice_set)
    with open(mysets_file, "w") as f:
        json.dump(lista_set, f, indent=2)

def apri_istruzioni_set(codice_set, nome_set=None):
    """Apre in browser la pagina del set direttamente sulla sezione istruzioni (#bi)."""
    if not codice_set:
        messagebox.showwarning("Istruzioni", "Codice set non disponibile")
        return

    slug = ""
    if nome_set:
        slug = re.sub(r"[^a-z0-9]+", "-", nome_set.lower()).strip("-")

    if slug:
        url = f"https://rebrickable.com/sets/{codice_set}/{slug}/#bi"
    else:
        url = f"https://rebrickable.com/sets/{codice_set}/#bi"

    try:
        webbrowser.open_new_tab(url)
    except Exception as e:
        messagebox.showerror("Istruzioni", f"Impossibile aprire il browser: {e}")

def apri_gestione_set():
    global gestione_win, frame_inner, aggiorna_lista_func, apri_dettaglio_set_func

    if not sets:
        messagebox.showinfo("Gestione Set", "Nessun set caricato!")
        return

    if gestione_win is not None and gestione_win.winfo_exists():
        gestione_win.lift()
        gestione_win.focus_force()
        return

    gestione_win = tk.Toplevel(root)
    gestione_win.withdraw()
    gestione_win.title("Gestione Set")

    canvas = tk.Canvas(gestione_win)
    bind_mousewheel_scroll(canvas)
    canvas.pack(side="left", fill="both", expand=True)

    scrollbar = tk.Scrollbar(gestione_win, command=canvas.yview)
    scrollbar.pack(side="right", fill="y")
    canvas.configure(yscrollcommand=scrollbar.set)

    frame_inner = tk.Frame(canvas)
    canvas.create_window((0, 0), window=frame_inner, anchor="nw")

    def on_configure_gestione(*_):
        canvas.configure(scrollregion=canvas.bbox("all"))
        auto_resize_window(
            gestione_win,
            frame_inner,
            scrollbar,
            min_w=520,
            min_h=280,
            max_w=1300,
            max_h=900,
            extra_w=30,
            extra_h=80
        )

    frame_inner.bind("<Configure>", lambda e: on_configure_gestione())

    # =========================
    # 🔥 DETTAGLIO SET CON IMMAGINI A GRIGLIA
    # =========================
    def apri_dettaglio_set(nome_set):
        if nome_set not in sets:
            return

        # Usa gestione_win come master solo se esiste ancora, altrimenti root
        master = gestione_win if gestione_win is not None and gestione_win.winfo_exists() else root
        dettaglio_win = tk.Toplevel(master)
        dettaglio_win.withdraw()
        numero = numerazione_set.get(nome_set, "?")

        def aggiorna_titolo_dettaglio():
            pezzi_messi = sum(d.get("used", 0) for d in sets[nome_set].values())
            pezzi_totali = sum(d.get("total", 0) for d in sets[nome_set].values())
            dettaglio_win.title(
                f"Dettaglio: {numero}. {nome_set} | Pezzi messi: {pezzi_messi} | Pezzi totali: {pezzi_totali}"
            )

        aggiorna_titolo_dettaglio()

        def ricarica_griglia():
            """Ricarica la griglia dei pezzi e restituisce riferimenti per aggiornamenti veloci"""
            dettaglio_ui_refs = {}
            
            for w in frame.winfo_children():
                w.destroy()

            # Mostra in cima i pezzi completati, poi i mancanti.
            pezzi_ordinati = sorted(
                sets[nome_set].items(),
                key=lambda item: (
                    0 if item[1].get("total", 0) > 0 and item[1].get("used", 0) >= item[1].get("total", 0) else 1,
                    item[0]
                )
            )

            for i, (pezzo, dati) in enumerate(pezzi_ordinati):
                row = i // COLS
                col = i % COLS

                used = dati.get("used", 0)
                total = dati.get("total", 0)
                is_completo = total > 0 and used >= total

                # --- frame singolo pezzo (tipo card)
                card_bg = "#d4edda" if is_completo else "SystemButtonFace"
                card = tk.Frame(frame, bd=1, relief="solid", padx=5, pady=5, cursor="hand2", bg=card_bg)
                card.grid(row=row, column=col, padx=5, pady=5, sticky="n")

                # --- immagine
                img_tk, img_dark_tk = load_image_pair(pezzo)
                if img_tk is None:
                    img = Image.new("RGB", (120, 120), color="lightgray")
                    img_tk = ImageTk.PhotoImage(img)

                lbl_img = tk.Label(card, image=img_tk, bg=card_bg)
                lbl_img.pack()
                lbl_img.image = img_tk

                # --- nome pezzo sotto l'immagine
                lbl_nome = tk.Label(card, text=pezzo, wraplength=120, justify="center", font=("Arial", 11, "bold"), bg=card_bg)
                lbl_nome.pack(pady=(5,0))

                # --- quantità
                lbl_quant = tk.Label(card, text=f"{used}/{total}", font=("Arial", 11, "bold"), fg="green" if is_completo else "black", bg=card_bg)
                lbl_quant.pack()

                # --- bind click sulla card
                card.bind("<Button-1>", lambda e, pk=pezzo: modifica_quantita(pk))
                lbl_img.bind("<Button-1>", lambda e, pk=pezzo: modifica_quantita(pk))
                lbl_nome.bind("<Button-1>", lambda e, pk=pezzo: modifica_quantita(pk))
                lbl_quant.bind("<Button-1>", lambda e, pk=pezzo: modifica_quantita(pk))

                # Salva riferimenti per aggiornamenti veloci
                dettaglio_ui_refs[pezzo] = {
                    "card": card,
                    "img": lbl_img,
                    "nome": lbl_nome,
                    "quant": lbl_quant
                }

            canvas.update_idletasks()
            canvas.configure(scrollregion=canvas.bbox("all"))
            return dettaglio_ui_refs

        def aggiorna_pezzo_dettaglio(pezzo_key, dettaglio_ui_refs):
            """Aggiorna SOLO il pezzo specifico nella finestra dettaglio - VELOCISSIMO!"""
            if pezzo_key not in dettaglio_ui_refs or pezzo_key not in sets[nome_set]:
                return
            
            dati = sets[nome_set][pezzo_key]
            used = dati.get("used", 0)
            total = dati.get("total", 0)
            is_completo = total > 0 and used >= total
            card_bg = "#d4edda" if is_completo else "SystemButtonFace"

            refs = dettaglio_ui_refs[pezzo_key]
            refs["quant"].config(text=f"{used}/{total}", fg="green" if is_completo else "black", bg=card_bg)
            refs["card"].config(bg=card_bg)
            refs["img"].config(bg=card_bg)
            refs["nome"].config(bg=card_bg)

        def modifica_quantita(pezzo_key):
            def conferma():
                try:
                    nuova_qty = int(entry_qty.get())
                    if nuova_qty < 0:
                        messagebox.showerror("Errore", "La quantità non può essere negativa")
                        return
                    if nuova_qty > sets[nome_set][pezzo_key]["total"]:
                        messagebox.showerror("Errore", f"Massimo disponibile: {sets[nome_set][pezzo_key]['total']}")
                        return
                    
                    # Aggiorna la quantità
                    sets[nome_set][pezzo_key]["used"] = nuova_qty
                    
                    # Aggiorna global_parts
                    if pezzo_key in global_parts:
                        global_parts[pezzo_key]["used"] = nuova_qty
                    
                    # Salva su file
                    save_lego_data()
                    
                    # Chiudi finestra
                    mod_win.destroy()
                    
                    # 🔥 AGGIORNAMENTO VELOCE invece di ricarica completa
                    aggiorna_pezzo_dettaglio(pezzo_key, dettaglio_ui_refs)
                    aggiorna_titolo_dettaglio()
                    
                    # Aggiorna altre UI
                    aggiorna_riepilogo()
                    aggiorna_pezzo_griglia(pezzo_key)
                    aggiorna_log_ui()
                    
                except ValueError:
                    messagebox.showerror("Errore", "Inserisci un numero valido")

            mod_win = tk.Toplevel(dettaglio_win)
            mod_win.title(f"Modifica: {pezzo_key}")
            mod_win.geometry("300x150")

            attuale = sets[nome_set][pezzo_key]["used"]
            massimo = sets[nome_set][pezzo_key]["total"]

            tk.Label(mod_win, text=f"Quantità per: {pezzo_key}", font=("Arial", 15, "bold")).pack(pady=10)
            tk.Label(mod_win, text=f"Massimo disponibile: {massimo}", font=("Arial", 15)).pack()

            frame_entry = tk.Frame(mod_win)
            frame_entry.pack(pady=5)

            tk.Label(frame_entry, text="Nuova quantità:", font=("Arial", 15)).pack(side="left", padx=5)
            entry_qty = tk.Entry(frame_entry, width=10, justify="center")
            entry_qty.pack(side="left", padx=5)
            entry_qty.insert(0, str(attuale))
            entry_qty.select_range(0, tk.END)
            entry_qty.focus()

            frame_btn = tk.Frame(mod_win)
            frame_btn.pack(pady=10)

            tk.Button(frame_btn, text="OK", command=conferma).pack(side="left", padx=5)
            tk.Button(frame_btn, text="Annulla", command=mod_win.destroy).pack(side="left", padx=5)

            mod_win.resizable(False, False)

            canvas.update_idletasks()
            canvas.configure(scrollregion=canvas.bbox("all"))

        canvas = tk.Canvas(dettaglio_win)
        bind_mousewheel_scroll(canvas)
        canvas.pack(side="left", fill="both", expand=True)

        scrollbar = tk.Scrollbar(dettaglio_win, command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=scrollbar.set)

        frame = tk.Frame(canvas)
        canvas.create_window((0, 0), window=frame, anchor="nw")

        def on_configure_dettaglio(*_):
            canvas.configure(scrollregion=canvas.bbox("all"))
            auto_resize_window(
                dettaglio_win,
                frame,
                scrollbar,
                min_w=780,
                min_h=420,
                max_w=1700,
                max_h=1000,
                extra_w=40,
                extra_h=90
            )

        frame.bind("<Configure>", lambda e: on_configure_dettaglio())

        # 🔢 numero colonne griglia
        COLS = 8
        dettaglio_ui_refs = ricarica_griglia()
        on_configure_dettaglio()
        dettaglio_win.deiconify()
        dettaglio_win.lift()
        dettaglio_win.focus_force()

    apri_dettaglio_set_func = apri_dettaglio_set

    # =========================
    # --- aggiorna lista ---
    # =========================
    def aggiorna_lista():
        if frame_inner is None:
            return
        try:
            if not frame_inner.winfo_exists():
                return
        except:
            return

        for w in frame_inner.winfo_children():
            w.destroy()

        # Ordina i set secondo numerazione_set
        ordinati = sorted(
            ((nome, s) for nome, s in sets.items()),
            key=lambda x: numerazione_set.get(x[0], 9999)
        )

        def sposta_set(nome_set, delta):
            codice_set = set_codes.get(nome_set)
            if not codice_set:
                risultato.config(text=f"Codice set non trovato per {nome_set}", fg="orange")
                return

            codes = load_mysets()
            if codice_set not in codes:
                risultato.config(text=f"{codice_set} non presente in mysets.json", fg="orange")
                return

            idx = codes.index(codice_set)
            new_idx = idx + delta
            if new_idx < 0 or new_idx >= len(codes):
                return

            codes[idx], codes[new_idx] = codes[new_idx], codes[idx]
            save_mysets(codes)

            log_pezzi.append({
                "event": "set_move",
                "set": nome_set,
                "set_code": codice_set,
                "from_pos": idx + 1,
                "to_pos": new_idx + 1
            })
            print(
                f"[LOGDBG] append set_move: code={codice_set} set={nome_set} "
                f"from={idx + 1} to={new_idx + 1}"
            )
            save_log()

            aggiorna_numerazione_set()
            aggiorna_lista()
            aggiorna_riepilogo()
            aggiorna_titolo()
            aggiorna_griglia(force=True)
            aggiorna_log_ui()

            if (
                stato_pezzo_win is not None
                and stato_pezzo_win.winfo_exists()
                and stato_pezzo_key
            ):
                apri_stato_pezzo(stato_pezzo_key)

            dir_txt = "su" if delta < 0 else "giu"
            risultato.config(text=f"Spostato {codice_set} {dir_txt}", fg="green")

        codici_file = load_mysets()
        index_codice = {c: i for i, c in enumerate(codici_file)}
        for nome, s in ordinati:
            tot = sum(v["total"] for v in s.values())
            use = sum(v["used"] for v in s.values())

            f = tk.Frame(frame_inner)
            f.pack(fill="x", pady=2, padx=2)

            codice = set_codes.get(nome, nome)
            numero = numerazione_set.get(nome, "?")

            testo_label = f"{numero}. {nome} - {codice} → {use}/{tot}"

            # Miniatura set
            thumb_lbl = tk.Label(f, width=SET_THUMB_SIZE[0], height=SET_THUMB_SIZE[1],
                                 bg="#eeeeee", relief="flat", cursor="hand2")
            thumb_lbl.pack(side="left", padx=(2, 6))
            load_set_thumbnail(codice, thumb_lbl)
            thumb_lbl.bind("<Button-1>", lambda e, c=codice, n=nome: apri_anteprima_set_grande(c, n))

            lbl = tk.Label(f, text=testo_label, anchor="w", cursor="hand2")
            lbl.pack(side="left", fill="x", expand=True)

            # 🔥 CLICK APRE DETTAGLIO
            lbl.bind("<Button-1>", lambda e, nome_set=nome: apri_dettaglio_set(nome_set))

            def elimina(n=codice, nome_set=nome):
                if messagebox.askyesno("Conferma", f"Cancellare {n}?"):
                    sets.pop(nome_set, None)
                    elimina_set_da_file(n)
                    aggiorna_numerazione_set()
                    aggiorna_lista()
                    aggiorna_riepilogo()

            def azzera_pezzi(nome_set=nome):
                if messagebox.askyesno("Conferma", f"Azzera i pezzi usati per {nome_set}?"):
                    for pezzo in sets[nome_set].values():
                        pezzo["used"] = 0

                    # Aggiorna anche i dati salvati
                    saved = load_lego_data()
                    if nome_set in saved:
                        for pezzo in saved[nome_set]:
                            saved[nome_set][pezzo] = 0
                        # Salva i dati aggiornati
                        with open(lego_data_file, "w") as f:
                            json.dump(saved, f, indent=2)

                    aggiorna_global()
                    aggiorna_lista()
                    aggiorna_riepilogo()
                    aggiorna_filtri_set()
                    save_lego_data(force=True)
                    aggiorna_griglia(force=True)

            idx_codice = index_codice.get(codice)
            puo_salire = idx_codice is not None and idx_codice > 0
            puo_scendere = idx_codice is not None and idx_codice < (len(codici_file) - 1)

            tk.Button(
                f,
                text="↑",
                width=3,
                state="normal" if puo_salire else "disabled",
                command=lambda nome_set=nome: sposta_set(nome_set, -1)
            ).pack(side="right", padx=(2, 0))
            tk.Button(
                f,
                text="↓",
                width=3,
                state="normal" if puo_scendere else "disabled",
                command=lambda nome_set=nome: sposta_set(nome_set, 1)
            ).pack(side="right", padx=(2, 6))

            tk.Button(f, text="Elimina", command=elimina).pack(side="right")
            tk.Button(f, text="Azzera", command=azzera_pezzi).pack(side="right", padx=5)
            tk.Button(f, text="Istruzioni", command=lambda c=codice, n=nome: apri_istruzioni_set(c, n)).pack(side="right", padx=5)

            # Checkbox Disabilita set
            var_dis = tk.BooleanVar(value=(nome in disabled_sets))
            def _on_toggle_disabilita(n=nome, var=var_dis):
                if var.get():
                    disabled_sets.add(n)
                else:
                    disabled_sets.discard(n)
                save_disabled_sets()
                aggiorna_global()
                aggiorna_filtri_set()
                aggiorna_griglia(force=True)
                aggiorna_riepilogo()
                aggiorna_lista()
            chk = tk.Checkbutton(
                f,
                text="Disabilita",
                variable=var_dis,
                command=_on_toggle_disabilita,
                fg="gray"
            )
            chk.pack(side="right", padx=(8, 4))

    # --- chiusura ---
    def on_close_gestione():
        global frame_inner, aggiorna_lista_func
        frame_inner = None
        aggiorna_lista_func = None
        gestione_win.destroy()

    gestione_win.protocol("WM_DELETE_WINDOW", on_close_gestione)

    aggiorna_lista()
    aggiorna_lista_func = aggiorna_lista
    aggiorna_riepilogo()
    aggiorna_titolo()
    on_configure_gestione()
    gestione_win.deiconify()
    

# ------------------------
# PEZZI TOTALI
# ------------------------
def aggiorna_titolo():
    # Totale pezzi fisici
    totali = sum(v["total"] for s in sets.values() for v in s.values())
    # Totale pezzi usati
    usati = sum(v["used"] for s in sets.values() for v in s.values())
    
    # Nr. set
    totale_set_file = len(numerazione_set)   # tutti i set salvati nel file
    caricati_set = len(sets)                # set effettivamente caricati
    mancanti_set = totale_set_file - caricati_set

    print(f"LegoFinder - Pezzi usati: {usati}/{totali} | Set caricati: {caricati_set}/{totale_set_file} | Mancanti: {mancanti_set}")

    root.title(
        f"LEGO Smista PRO 🔥 - Set caricati: {caricati_set}/{totale_set_file} "
        f"({mancanti_set} mancanti) - Pezzi usati: {usati} - Pezzi totali: {totali}"
    )

# ------------------------
# RIEPILOGO
# ------------------------
riepilogo_win = None
frame_info_win = None

def apri_riepilogo():
    global riepilogo_win, frame_info_win, aggiorna_lista_func

    if riepilogo_win is None or not tk.Toplevel.winfo_exists(riepilogo_win):
        riepilogo_win = tk.Toplevel(root)
        riepilogo_win.withdraw()
        riepilogo_win.title("Riepilogo Set")

        canvas = tk.Canvas(riepilogo_win)
        bind_mousewheel_scroll(canvas)
        canvas.pack(side="left", fill="both", expand=True)

        scrollbar = tk.Scrollbar(riepilogo_win, command=canvas.yview)
        scrollbar.pack(side="right", fill="y")

        canvas.configure(yscrollcommand=scrollbar.set)

        frame_info_win = tk.Frame(canvas)
        canvas.create_window((0, 0), window=frame_info_win, anchor="nw")

        def on_configure_riepilogo(*_):
            canvas.configure(scrollregion=canvas.bbox("all"))
            auto_resize_window(
                riepilogo_win,
                frame_info_win,
                scrollbar,
                min_w=460,
                min_h=240,
                max_w=1200,
                max_h=900,
                extra_w=30,
                extra_h=80
            )

        frame_info_win.bind("<Configure>", lambda e: on_configure_riepilogo())

        aggiorna_riepilogo()
        on_configure_riepilogo()
        riepilogo_win.deiconify()
    else:
        riepilogo_win.lift()

def aggiorna_riepilogo():
    global frame_info_win
    # Controllo se il frame esiste e non è stato distrutto
    if frame_info_win is None or not tk.Toplevel.winfo_exists(frame_info_win):
        return

    for w in frame_info_win.winfo_children():
        w.destroy()

    # Ordina i set per completamento (dal piu completo al meno completo)
    def completion_ratio(item):
        _, s = item
        tot = sum(v["total"] for v in s.values())
        use = sum(v["used"] for v in s.values())
        return (use / tot) if tot > 0 else 0

    ordinati = sorted(
        ((nome, s) for nome, s in sets.items()),
        key=lambda x: (completion_ratio(x), -numerazione_set.get(x[0], 9999)),
        reverse=True
    )
    for nome, s in ordinati:
        tot = sum(v["total"] for v in s.values())
        use = sum(v["used"] for v in s.values())
        perc = int((use / tot) * 100) if tot > 0 else 0

        f = tk.Frame(frame_info_win)
        f.pack(fill="x", padx=4, pady=2)

        numero = numerazione_set.get(nome, "?")
        tk.Label(f, text=f"{numero}. {nome} → {use}/{tot} ({perc}%)", anchor="w").pack(fill="x")

        # Barra verde di completamento 0-100
        bar_w = 360
        bar_h = 12
        bar = tk.Canvas(f, width=bar_w, height=bar_h, highlightthickness=0)
        bar.pack(anchor="w", pady=(2, 0))
        bar.create_rectangle(0, 0, bar_w, bar_h, fill="#e6e6e6", outline="")
        fill_w = int(bar_w * (perc / 100))
        if fill_w > 0:
            bar.create_rectangle(0, 0, fill_w, bar_h, fill="#21a366", outline="")

    if riepilogo_win is not None and tk.Toplevel.winfo_exists(riepilogo_win):
        auto_resize_window(
            riepilogo_win,
            frame_info_win,
            min_w=460,
            min_h=240,
            max_w=1200,
            max_h=900,
            extra_w=30,
            extra_h=80
        )



# ------------------------
# LOG + GRIGLIA PEZZI CORRETTO
# ------------------------

log_file = os.path.join(script_dir, "logpezzi.json")

# Creo il file vuoto se non esiste
if not os.path.exists(log_file):
    with open(log_file, "w") as f:
        json.dump([], f)

log_pezzi = []
log_win = None
frame_log_inner = None
batch_win = None
batch_queue = []
batch_index = 0
batch_refresh_func = None
magic_win = None

def save_log(force=False):
    """Salva log con batch system - evita I/O eccessivo"""
    global last_save_time, save_pending
    now = time.time()
    
    if force or (now - last_save_time) >= SAVE_COOLDOWN:
        try:
            with open(log_file, "w") as f:
                json.dump(log_pezzi, f, indent=2)
            last_save_time = now
            save_pending = False
            print(f"[LOGDBG] save_log: scritto {len(log_pezzi)} eventi (force={force})")
        except Exception as e:
            print(f"[ERRORE] Salvataggio log: {e}")
    else:
        save_pending = True
        print(
            f"[LOGDBG] save_log: rinviato (cooldown), eventi={len(log_pezzi)} "
            f"dt={now - last_save_time:.2f}s"
        )

def load_log():
    global log_pezzi
    try:
        with open(log_file, "r") as f:
            log_pezzi = json.load(f)
            if not isinstance(log_pezzi, list):
                log_pezzi = []
        print(f"[LOGDBG] load_log: caricati {len(log_pezzi)} eventi da {log_file}")
    except:
        log_pezzi = []
        print(f"[LOGDBG] load_log: file non valido/non leggibile, reset a lista vuota")

# Carico log all'avvio
load_log()

# ------------------------
# LOG WINDOW
# ------------------------

def apri_log_pezzi():
    global log_win, frame_log_inner

    if log_win is None or not tk.Toplevel.winfo_exists(log_win):
        log_win = tk.Toplevel(root)
        log_win.withdraw()
        log_win.attributes("-topmost", True)
        log_win.title("Log Pezzi")

        # 🔥 FRAME TOP (per bottoni)
        frame_top_log = tk.Frame(log_win)
        frame_top_log.pack(fill="x", pady=2)
        
        tk.Button(frame_top_log, text="🗑 Cancella log", command=cancella_log)\
            .pack(side="left", padx=5, pady=5)

        # 🔥 CANVAS
        canvas = tk.Canvas(log_win)
        bind_mousewheel_scroll(canvas)
        canvas.pack(side="left", fill="both", expand=True)

        scrollbar = tk.Scrollbar(log_win, command=canvas.yview)
        scrollbar.pack(side="right", fill="y")

        canvas.configure(yscrollcommand=scrollbar.set)

        frame_log_inner = tk.Frame(canvas)
        canvas.create_window((0, 0), window=frame_log_inner, anchor="nw")

        def on_configure_log(*_):
            canvas.configure(scrollregion=canvas.bbox("all"))
            auto_resize_window(
                log_win,
                frame_log_inner,
                scrollbar,
                min_w=500,
                min_h=260,
                max_w=1200,
                max_h=950,
                extra_w=30,
                extra_h=90
            )

        frame_log_inner.bind("<Configure>", lambda e: on_configure_log())

        aggiorna_log_ui()
        on_configure_log()
        log_win.deiconify()

    else:
        log_win.lift()

def cancella_log():
    global log_pezzi
    if not messagebox.askyesno("Conferma", "Cancellare tutto il log?"):
        return

    log_pezzi = []
    print("[LOGDBG] cancella_log: log svuotato")
    save_log()
    aggiorna_log_ui()
    
def aggiorna_log_ui():
    """Aggiorna la finestra Log Pezzi con raggruppamento e colore completato
       L'ultimo pezzo cliccato va sempre in cima e permette Undo singolo per gruppo."""
    global frame_log_inner
    if frame_log_inner is None or not tk.Toplevel.winfo_exists(frame_log_inner):
        print(
            f"[LOGDBG] aggiorna_log_ui: skip (finestra log non pronta), eventi totali={len(log_pezzi)}"
        )
        return

    # cancello vecchi widget
    for w in frame_log_inner.winfo_children():
        w.destroy()

    # Events di spostamento set (mostrati come storico separato)
    eventi_set = []
    eventi_undo = []

    # raggruppo pezzi uguali (key+set)
    conteggio = {}
    for idx, item in enumerate(log_pezzi):
        if item.get("event") == "set_move":
            eventi_set.append((idx, item))
            continue
        if item.get("event") == "undo":
            eventi_undo.append((idx, item))
            continue
        if "key" not in item or "set" not in item:
            continue
        chiave = (item["key"], item["set"])
        count = item.get("count", 1)
        if chiave not in conteggio:
            conteggio[chiave] = {
                "used": item["used"],
                "total": item["total"],
                "last_index": idx,
                "count": count
            }
        else:
            conteggio[chiave]["count"] += count
            conteggio[chiave]["used"] = item["used"]
            conteggio[chiave]["last_index"] = idx

    # ordino per ultimo inserimento, dall'ultimo cliccato in cima
    lista = sorted(conteggio.items(), key=lambda x: x[1]["last_index"], reverse=True)
    print(
        f"[LOGDBG] aggiorna_log_ui: righe pezzi={len(lista)} "
        f"eventi_set={len(eventi_set)} eventi_undo={len(eventi_undo)} eventi_totali={len(log_pezzi)}"
    )

    for (key, nome_set), data in lista:
        f = tk.Frame(frame_log_inner)
        f.pack(fill="x", pady=1)

        # immagine
        img_tk, img_dark_tk = load_image_pair(key)
        if img_tk:
            lbl_img = tk.Label(f, image=img_tk)
            lbl_img.image = img_tk  # 🔥 mantieni riferimento
            lbl_img.pack(side="left")

        numero_set = numerazione_set.get(nome_set, "?")
        testo = f"{data['count']}x → {numero_set}. {nome_set} ({data['used']}/{data['total']})"
        colore = "green" if data["used"] >= data["total"] else "black"

        lbl = tk.Label(f, text=testo, fg=colore)
        lbl.pack(side="left")

        def ripristina_log(k=key, ns=nome_set):
            # trova l'ultimo log corrispondente e decrementa di 1
            for i in reversed(range(len(log_pezzi))):
                item = log_pezzi[i]
                if item.get("event") == "set_move":
                    continue
                if "key" not in item or "set" not in item:
                    continue
                if item["key"] == k and item["set"] == ns:
                    # decremento count singolo
                    item_count = item.get("count", 1)
                    item_count -= 1
                    # decrementa i used nella struttura principale
                    sets[ns][k]["used"] = max(sets[ns][k]["used"] - 1, 0)
                    if item_count <= 0:
                        log_pezzi.pop(i)
                    else:
                        item["count"] = item_count
                        item["used"] = sets[ns][k]["used"]
                    break

            save_log()
            aggiorna_global()
            aggiorna_log_ui()
            aggiorna_riepilogo()
            aggiorna_pezzo_griglia(k)
            if aggiorna_lista_func:
                aggiorna_lista_func()
            aggiorna_label_movimento()
            print(f"[LOGDBG] ripristina_log: k={k} set={ns}")

        tk.Button(f, text="Ripristina", command=ripristina_log).pack(side="right", padx=5)

    if eventi_undo:
        tk.Label(
            frame_log_inner,
            text="Storico undo",
            font=("Arial", 10, "bold"),
            anchor="w"
        ).pack(fill="x", pady=(8, 2))

        for _, item in sorted(eventi_undo, key=lambda x: x[0], reverse=True):
            f = tk.Frame(frame_log_inner)
            f.pack(fill="x", pady=1)

            k = item.get("key", "?")
            ns = item.get("set", "?")
            used = item.get("used", "?")
            total = item.get("total", "?")
            ts = item.get("timestamp", "")
            numero_set = numerazione_set.get(ns, "?")
            testo = f"↩ {numero_set}. {ns}  {k}  ({used}/{total})"
            if ts:
                testo += f"  [{ts}]"
            tk.Label(f, text=testo, fg="#8B4513", anchor="w").pack(side="left", fill="x", expand=True)

    if eventi_set:
        tk.Label(
            frame_log_inner,
            text="Storico spostamenti set",
            font=("Arial", 10, "bold"),
            anchor="w"
        ).pack(fill="x", pady=(8, 2))

        for _, item in sorted(eventi_set, key=lambda x: x[0], reverse=True):
            f = tk.Frame(frame_log_inner)
            f.pack(fill="x", pady=1)

            nome_set = item.get("set", "?")
            codice_set = item.get("set_code", "?")
            from_pos = item.get("from_pos", "?")
            to_pos = item.get("to_pos", "?")
            testo = f"Set {codice_set} ({nome_set}): {from_pos} -> {to_pos}"
            tk.Label(f, text=testo, fg="#1f4f99", anchor="w").pack(side="left", fill="x", expand=True)

    if log_win is not None and tk.Toplevel.winfo_exists(log_win):
        auto_resize_window(
            log_win,
            frame_log_inner,
            min_w=500,
            min_h=260,
            max_w=1200,
            max_h=950,
            extra_w=30,
            extra_h=90
        )

def mostra_immagine_grande(k):
    """Apre una finestra con l'immagine grande del pezzo (usata per No Color/Any Color)."""
    path = get_image_path(k)
    if not os.path.exists(path):
        # prova a scaricarla prima
        download_image(k)
    if not os.path.exists(path):
        return

    win = tk.Toplevel(root)
    win.title(k)
    win.resizable(True, True)

    try:
        img = Image.open(path)
        max_size = 500
        img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(img)
        lbl = tk.Label(win, image=photo, cursor="hand2")
        lbl.image = photo
        lbl.pack(padx=10, pady=10)
    except Exception:
        tk.Label(win, text="Immagine non disponibile").pack(padx=20, pady=20)

    part_num = k.split("_")[0]
    tk.Label(win, text=f"Pezzo: {part_num}  |  Colore: No Color/Any Color",
             font=("Arial", 12)).pack(pady=(0, 10))
    tk.Button(win, text="Chiudi", command=win.destroy).pack(pady=(0, 10))


def apri_stato_pezzo(k):
    """Mostra in quali set e con quali quantità è presente il pezzo selezionato."""
    global stato_pezzo_win, stato_pezzo_key
    stato_pezzo_key = k
    if stato_pezzo_win is not None and stato_pezzo_win.winfo_exists():
        stato_pezzo_win.destroy()

    win = tk.Toplevel(root)
    stato_pezzo_win = win
    win.withdraw()
    win.title(f"Stato pezzo: {k}")

    def chiudi_stato_pezzo():
        global stato_pezzo_win, stato_pezzo_key
        stato_pezzo_key = None
        if stato_pezzo_win is not None and stato_pezzo_win.winfo_exists():
            stato_pezzo_win.destroy()
        stato_pezzo_win = None

    frame_top = tk.Frame(win)
    frame_top.pack(fill="x", pady=5)
    tk.Button(frame_top, text="Chiudi", command=chiudi_stato_pezzo).pack(side="right", padx=10)

    canvas_local = tk.Canvas(win)
    bind_mousewheel_scroll(canvas_local)
    canvas_local.pack(side="left", fill="both", expand=True)

    scrollbar_local = tk.Scrollbar(win, command=canvas_local.yview)
    scrollbar_local.pack(side="right", fill="y")
    canvas_local.configure(yscrollcommand=scrollbar_local.set)

    frame_inner_local = tk.Frame(canvas_local)
    canvas_local.create_window((0, 0), window=frame_inner_local, anchor="nw")
    frame_inner_local.bind(
        "<Configure>",
        lambda e: canvas_local.configure(scrollregion=canvas_local.bbox("all"))
    )

    disponibili = []
    for nome in get_all_set_names_for_piece(k):
        s = sets[nome]
        if k in s:
            v = s[k]
            numero = numerazione_set.get(nome, "?")
            disponibili.append((numero, nome, v))

    if not disponibili:
        tk.Label(frame_inner_local, text="Pezzo non presente nei set caricati").grid(
            row=0, column=0, padx=8, pady=8, sticky="w"
        )
    else:
        frame_inner_local.grid_columnconfigure(0, weight=1)
        frame_inner_local.grid_columnconfigure(1, weight=0)
        frame_inner_local.grid_columnconfigure(2, weight=0)

        for riga, (numero, nome, v) in enumerate(disponibili):
            used = v.get("used", 0)
            total = v.get("total", 0)
            completo = used >= total and total > 0
            stato = "Completo" if completo else "Mancante"
            stato_colore = "green" if completo else "orange"

            tk.Label(
                frame_inner_local,
                text=f"{numero}. {nome}",
                anchor="w"
            ).grid(row=riga, column=0, sticky="w", padx=(5, 8), pady=2)

            tk.Label(
                frame_inner_local,
                text=f"{used}/{total}",
                width=8,
                anchor="e"
            ).grid(row=riga, column=1, sticky="e", padx=(0, 8), pady=2)

            tk.Label(
                frame_inner_local,
                text=stato,
                fg=stato_colore,
                width=9,
                anchor="w"
            ).grid(row=riga, column=2, sticky="w", padx=(0, 6), pady=2)

    win.update_idletasks()
    larghezza = min(max(520, frame_inner_local.winfo_reqwidth() + scrollbar_local.winfo_reqwidth() + 30), 1400)
    altezza = min(max(170, frame_inner_local.winfo_reqheight() + 80), 650)
    win.geometry(f"{larghezza}x{altezza}")
    win.protocol("WM_DELETE_WINDOW", chiudi_stato_pezzo)
    win.deiconify()

def _build_batch_queue():
    """Costruisce la coda pezzi mancanti in base ai filtri correnti (set+colore)."""
    source_parts, source_set = get_source_parts()
    selected_color = color_var.get() if color_var is not None else "Tutti"

    queue = []
    for k, v in source_parts.items():
        colore = k.split("_")[1]
        if selected_color != "Tutti":
            if selected_color == "Trans":
                if not is_trans_color(colore):
                    continue
            elif colore != selected_color:
                continue

        part_num = k.rsplit("_", 1)[0]
        if not passa_filtri_dimensionali(part_num):
            continue

        used, total = get_piece_counts(k, source_set)
        missing = max(total - used, 0)
        if missing > 0:
            queue.append((k, v, used, total, missing))

    current_sort = ordine_var.get() if ordine_var is not None else "Stud"
    if current_sort == "Stud":
        queue.sort(
            key=lambda item: (
                lunghezza_pezzo_valore(item[1].get("name", ""), item[0].rsplit("_", 1)[0]),
                get_weight_g(item[0].rsplit("_", 1)[0]) or -1.0,
                item[3]
            ),
            reverse=True
        )
    elif current_sort == "Peso":
        queue.sort(
            key=lambda item: (
                get_weight_g(item[0].rsplit("_", 1)[0]) or -1.0,
                lunghezza_pezzo_valore(item[1].get("name", ""), item[0].rsplit("_", 1)[0]),
                item[3]
            ),
            reverse=True
        )
    elif current_sort == "Dimensioni":
        queue.sort(
            key=lambda item: (
                get_pack_max_cm(item[0].rsplit("_", 1)[0]) or -1.0,
                get_weight_g(item[0].rsplit("_", 1)[0]) or -1.0,
                lunghezza_pezzo_valore(item[1].get("name", ""), item[0].rsplit("_", 1)[0])
            ),
            reverse=True
        )
    else:
        # Quantita
        queue.sort(key=lambda item: (item[3], item[4]), reverse=True)
    return queue

def apri_modalita_batch_colore():
    """Modalita rapida: un pezzo alla volta per smistamento per colore."""
    global batch_win, batch_queue, batch_index, batch_refresh_func

    if batch_win is not None and batch_win.winfo_exists():
        batch_win.lift()
        return

    batch_queue = _build_batch_queue()
    batch_index = 0

    batch_win = tk.Toplevel(root)
    batch_win.withdraw()
    batch_win.title("Batch Colore")

    frame_top = tk.Frame(batch_win)
    frame_top.pack(fill="x", pady=6)

    lbl_title = tk.Label(frame_top, text="", font=("Arial", 28, "bold"), anchor="w")
    lbl_title.pack(side="left", padx=10)

    lbl_prog = tk.Label(frame_top, text="", font=("Arial", 24, "bold"), anchor="e")
    lbl_prog.pack(side="right", padx=10)

    frame_center = tk.Frame(batch_win)
    frame_center.pack(fill="both", expand=True, padx=10, pady=4)

    frame_piece = tk.Frame(frame_center)
    frame_piece.pack(fill="x", pady=5)

    lbl_img = tk.Label(frame_piece)
    lbl_img.pack(side="left", padx=(0, 14), anchor="n")

    lbl_piece_meta = tk.Label(
        frame_piece,
        text="",
        font=("Arial", 20, "bold"),
        justify="left",
        anchor="nw"
    )
    lbl_piece_meta.pack(side="left", anchor="n")

    lbl_key = tk.Label(frame_center, text="", font=("Arial", 30, "bold"))
    lbl_key.pack()

    lbl_dest = tk.Label(
        frame_center,
        text="",
        font=("Arial", 34, "bold"),
        fg="#0a7a22",
        justify="center",
        wraplength=1000
    )
    lbl_dest.pack(pady=(6, 6), fill="x")

    lbl_qty = tk.Label(frame_center, text="", font=("Arial", 30, "bold"))
    lbl_qty.pack(pady=(4, 2))

    txt_sets = tk.Text(
        frame_center,
        height=9,
        font=("Arial", 16),
        wrap="word",
        bd=1,
        relief="solid"
    )
    txt_sets.pack(fill="both", expand=True, pady=(6, 8))
    txt_sets.tag_config("ok", foreground="green")
    txt_sets.tag_config("target", foreground="orange")
    txt_sets.tag_config("pending", foreground="black")
    txt_sets.config(state="disabled")

    frame_buttons = tk.Frame(batch_win)
    frame_buttons.pack(fill="x", pady=6)

    def current_item():
        if not batch_queue:
            return None
        if batch_index < 0 or batch_index >= len(batch_queue):
            return None
        return batch_queue[batch_index]

    def refresh_current_item_state():
        """Aggiornamento incrementale della coda per migliorare reattivita batch."""
        global batch_queue, batch_index
        item = current_item()
        if item is None:
            return

        k, v, _, _, _ = item
        used, total = get_piece_counts(k, grid_source_set)
        missing = max(total - used, 0)

        if missing <= 0:
            batch_queue.pop(batch_index)
            if batch_queue:
                batch_index = min(batch_index, len(batch_queue) - 1)
            else:
                batch_index = 0
            return

        batch_queue[batch_index] = (k, v, used, total, missing)

    def render():
        item = current_item()
        if item is None:
            lbl_title.config(text="Batch completato")
            lbl_prog.config(text="0/0")
            lbl_img.config(image="")
            lbl_img.image = None
            lbl_piece_meta.config(text="")
            lbl_key.config(text="Nessun pezzo mancante con i filtri correnti")
            lbl_dest.config(text="")
            lbl_qty.config(text="")
            txt_sets.config(state="normal")
            txt_sets.delete("1.0", tk.END)
            txt_sets.insert(tk.END, "Nessun set da mostrare\n")
            txt_sets.config(state="disabled")
            return

        k, v, used, total, missing = item
        numero = batch_index + 1
        tot = len(batch_queue)
        sort_label = ordine_var.get() if ordine_var is not None else "Stud"
        lbl_title.config(text=f"Colore: {color_var.get()} | Set: {set_var.get()} | Ord: {sort_label}")
        lbl_prog.config(text=f"{numero}/{tot}")

        img, _ = load_image_pair(k, size=280)
        if img is not None:
            lbl_img.config(image=img)
            lbl_img.image = img
        else:
            lbl_img.config(image="")
            lbl_img.image = None

        part_num = k.rsplit("_", 1)[0]
        dim_entry = get_piece_dimension_data(part_num)
        stud_txt = dim_entry.get("stud_dim") or (lunghezza_pezzo(v.get("name", ""), part_num) or "-")
        pack_txt = dim_entry.get("pack_dim", "-")
        peso_val = get_weight_g(part_num)
        peso_txt = f"{peso_val:.2f}g" if isinstance(peso_val, (int, float)) else "-"
        lbl_piece_meta.config(text=f"Stud: {stud_txt}\nPack: {pack_txt}\nPeso: {peso_txt}")

        lbl_key.config(text=k)
        lbl_qty.config(text=f"Mancano totali: {missing}")

        lines = []
        nomi_ordinati = sorted(
            get_all_set_names_for_piece(k),
            key=lambda nome: numerazione_set.get(nome, 9999)
        )
        for nome in nomi_ordinati:
            s = sets[nome]
            if k in s:
                vu = s[k]["used"]
                vt = s[k]["total"]
                stato = "OK" if vu >= vt else "Manca"
                num = numerazione_set.get(nome, "?")
                lines.append((nome, f"{num}. {nome}: {vu}/{vt} ({stato})\n", vu, vt))

        next_dest = get_next_destinazione_pezzo(k)
        if next_dest:
            nome_next, num_next, rem_next, _, total_next = next_dest
            lbl_dest.config(text=f"Metti in: {num_next}. {nome_next}\nPezzi mancanti: {rem_next} su {total_next}")
        else:
            lbl_dest.config(text="Pezzo completo in tutti i set")

        txt_sets.config(state="normal")
        txt_sets.delete("1.0", tk.END)
        for nome, riga, vu, vt in lines:
            if vu >= vt:
                tag = "ok"
            elif next_dest and nome == next_dest[0]:
                tag = "target"
            else:
                tag = "pending"
            txt_sets.insert(tk.END, riga, tag)
        txt_sets.config(state="disabled")

    def apply_qty(qty):
        item = current_item()
        if item is None:
            return
        k = item[0]
        click_pezzo(k, qty=qty)
        refresh_current_item_state()
        render()

    def refresh_from_filters(reset_to_first=False):
        global batch_queue, batch_index
        current = None if reset_to_first else current_item()
        keep_key = current[0] if current else None
        batch_queue = _build_batch_queue()

        if not batch_queue:
            batch_index = 0
            render()
            return

        if keep_key:
            found_index = None
            for i, item in enumerate(batch_queue):
                if item[0] == keep_key:
                    found_index = i
                    break
            if found_index is not None:
                batch_index = found_index
            else:
                batch_index = min(batch_index, len(batch_queue) - 1)
        else:
            batch_index = min(batch_index, len(batch_queue) - 1)

        render()

    def add_one():
        apply_qty(1)

    def add_five():
        apply_qty(5)

    def add_max():
        item = current_item()
        if item is None:
            return
        apply_qty(item[4])

    def next_item():
        global batch_index
        if not batch_queue:
            render()
            return
        batch_index = min(batch_index + 1, len(batch_queue) - 1)
        render()

    def prev_item():
        global batch_index
        if not batch_queue:
            render()
            return
        batch_index = max(batch_index - 1, 0)
        render()

    tk.Button(frame_buttons, text="+1", width=10, font=("Arial", 12, "bold"), command=add_one).pack(side="left", padx=5)
    tk.Button(frame_buttons, text="+5", width=10, font=("Arial", 12, "bold"), command=add_five).pack(side="left", padx=5)
    tk.Button(frame_buttons, text="Max", width=10, font=("Arial", 12, "bold"), command=add_max).pack(side="left", padx=5)
    tk.Button(frame_buttons, text="Prev", width=10, font=("Arial", 12, "bold"), command=prev_item).pack(side="left", padx=5)
    tk.Button(frame_buttons, text="Skip", width=10, font=("Arial", 12, "bold"), command=next_item).pack(side="left", padx=5)
    def close_batch():
        global batch_win, batch_refresh_func
        batch_refresh_func = None
        if batch_win is not None and batch_win.winfo_exists():
            batch_win.destroy()
        batch_win = None

    tk.Button(frame_buttons, text="Chiudi", width=10, font=("Arial", 12, "bold"), command=close_batch).pack(side="right", padx=5)

    lbl_shortcuts = tk.Label(
        batch_win,
        text="Scorciatoie: Spazio = +1 | Invio = +5 | M = Max | Freccia Sinistra = Prev | Freccia Destra = Skip | Esc = Chiudi",
        font=("Arial", 14, "bold"),
        anchor="w",
        justify="left"
    )
    lbl_shortcuts.pack(fill="x", padx=10, pady=(0, 8))

    batch_win.bind("<space>", lambda e: add_one())
    batch_win.bind("<Right>", lambda e: next_item())
    batch_win.bind("<Left>", lambda e: prev_item())
    batch_win.bind("<Return>", lambda e: add_five())
    batch_win.bind("m", lambda e: add_max())
    batch_win.bind("M", lambda e: add_max())
    batch_win.bind("<Escape>", lambda e: close_batch())
    batch_win.protocol("WM_DELETE_WINDOW", close_batch)

    batch_refresh_func = refresh_from_filters

    render()
    batch_win.update_idletasks()
    auto_resize_window(
        batch_win,
        frame_center,
        min_w=980,
        min_h=820,
        max_w=1400,
        max_h=1000,
        extra_w=40,
        extra_h=220
    )
    batch_win.deiconify()

# ------------------------
# CLICK PEZZO
# ------------------------
def click_pezzo(k, qty=1):
    global last_selected
    if lock_var is not None and lock_var.get():
        return  # 🔒 lucchetto attivo: blocca click
    prev_selected = last_selected
    last_selected = k  # 🔥 salva ultimo selezionato
    aggiorna_selezione_ui(prev_selected, last_selected)

    """Aggiorna pezzi, log e UI. qty=1 di default, ma può essere >1 da inserisci_quantita"""
    pezzo_modificato = False
    refresh_griglia = False

    for nome in get_available_set_names_for_piece(k):
        s = sets[nome]
        if k in s and s[k]["used"] < s[k]["total"]:
            aggiungi = min(qty, s[k]["total"] - s[k]["used"])
            s[k]["used"] += aggiungi
            v = s[k]
            numero = numerazione_set.get(nome, "?")

            global ultimo_movimento, ultimo_set_modificato
            ultimo_movimento = f"Ultimo pezzo → {numero}. {nome} ({v['used']}/{v['total']})"
            ultimo_set_modificato = nome

            log_pezzi.append({
                "event": "piece_click",
                "key": k,
                "set": nome,
                "used": v["used"],
                "total": v["total"],
                "count": aggiungi
            })
            print(
                f"[LOGDBG] append piece_click: key={k} set={nome} "
                f"count={aggiungi} used={v['used']}/{v['total']}"
            )

            save_log()

            if (solo_mancanti_var.get() or (togli_completi_var is not None and togli_completi_var.get())) and v["used"] >= v["total"]:
                refresh_griglia = True

            aggiorna_label_movimento()
            pezzo_modificato = True
            break

    if not pezzo_modificato:
        apri_stato_pezzo(k)
        on_hover_pezzo(None, k)
        return

    aggiorna_global()
    # 🔥 Salva subito su file per coerenza griglia
    save_lego_data(force=True)
    # Se il pezzo appena cliccato diventa completo con filtro attivo, serve
    # ricostruire l'elenco; altrimenti basta aggiornare la singola cella.
    if refresh_griglia:
        aggiorna_griglia(force=True)
    else:
        aggiorna_pezzo_griglia(k)
    aggiorna_log_ui()
    aggiorna_riepilogo()
    aggiorna_titolo()
    aggiorna_filtri_set()
    if aggiorna_lista_func:
        aggiorna_lista_func()
    on_hover_pezzo(None, k)

def apri_dettaglio_set_esistente(nome_set):
    global apri_dettaglio_set_func

    if nome_set not in sets:
        return

    if apri_dettaglio_set_func is None:
        apri_gestione_set()

    if apri_dettaglio_set_func is not None:
        apri_dettaglio_set_func(nome_set)

# ------------------------
# INSERISCI QUANTITÀ
# ------------------------
def inserisci_quantita(k):
    def costruisci_ui():
        for w in frame_inner.winfo_children():
            w.destroy()
        entries.clear()

        disponibili = []
        for nome, s in sets.items():
            if k in s and s[k]["used"] < s[k]["total"]:
                v = s[k]
                numero = numerazione_set.get(nome, "?")
                disponibili.append((numero, nome, v))

        if not disponibili:
            tk.Label(frame_inner, text="Nessun set disponibile").pack()
            win.update_idletasks()
            win.geometry("520x180")
            return

        for numero, nome, v in disponibili:
            f = tk.Frame(frame_inner)
            f.pack(fill="x", pady=2)

            max_add = v["total"] - v["used"]

            tk.Label(
                f,
                text=f"{numero}. {nome} ({v['used']}/{v['total']}) max:{max_add}",
                anchor="w"
            ).pack(side="left", padx=5)

            entry = tk.Entry(f, width=5, justify="center")
            entry.pack(side="right", padx=5)

            entries[nome] = (entry, max_add)

        win.update_idletasks()
        altezza = min(max(150, frame_inner.winfo_height() + 70), 600)
        win.geometry(f"420x{altezza}")

    def conferma():
        modificato = False
        for nome, (entry, max_add) in entries.items():
            try:
                qty = int(entry.get())
            except:
                continue
            if qty <= 0:
                continue

            qty = min(qty, max_add)
            if qty == 0:
                continue

            click_pezzo_generic(nome, k, qty)
            modificato = True

        if modificato:
            aggiorna_global()
            save_lego_data(force=True)
            aggiorna_filtri_set()
            aggiorna_pezzo_griglia(k)
            aggiorna_griglia(force=True)
            aggiorna_label_movimento()
            costruisci_ui()  # aggiorna UI

    # ------------------------
    # UI
    # ------------------------
    win = tk.Toplevel(root)
    win.title("Inserisci quantità per set")

    frame_top = tk.Frame(win)
    frame_top.pack(fill="x", pady=5)

    tk.Button(frame_top, text="OK", command=conferma).pack(side="left", padx=10)
    tk.Button(frame_top, text="Chiudi", command=win.destroy).pack(side="left", padx=5)

    canvas = tk.Canvas(win)
    bind_mousewheel_scroll(canvas)
    canvas.pack(side="left", fill="both", expand=True)

    scrollbar = tk.Scrollbar(win, command=canvas.yview)
    scrollbar.pack(side="right", fill="y")

    frame_inner = tk.Frame(canvas)
    canvas.create_window((0, 0), window=frame_inner, anchor="nw")

    frame_inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

    entries = {}

    costruisci_ui()


# Funzione interna per gestire inserimento quantità multipla senza duplicare codice
def click_pezzo_generic(nome_set, k, qty):
    s = sets[nome_set]
    aggiungi = min(qty, s[k]["total"] - s[k]["used"])
    if aggiungi <= 0:
        return

    s[k]["used"] += aggiungi
    v = s[k]
    numero = numerazione_set.get(nome_set, "?")

    global ultimo_movimento, ultimo_set_modificato
    ultimo_movimento = f"Ultimo pezzo → {numero}. {nome_set} ({v['used']}/{v['total']})"
    ultimo_set_modificato = nome_set

    log_pezzi.append({
        "event": "piece_click",
        "key": k,
        "set": nome_set,
        "used": v["used"],
        "total": v["total"],
        "count": aggiungi
    })
    print(
        f"[LOGDBG] append piece_click_generic: key={k} set={nome_set} "
        f"count={aggiungi} used={v['used']}/{v['total']}"
    )

    # Anche l'inserimento quantità deve persistere subito nello storico.
    save_log(force=True)

    aggiorna_global()
    save_lego_data(force=True)
    aggiorna_log_ui()
    aggiorna_riepilogo()
    aggiorna_filtri_set()
    if aggiorna_lista_func:
        aggiorna_lista_func()


# ------------------------
# MASTER DI RETE
# ------------------------
def _master_on_ui_thread(callback, timeout=12):
    """Esegue callback nel thread Tk e restituisce risultato/errore al server."""
    done = threading.Event()
    box = queue.Queue(maxsize=1)
    master_ui_requests.put((callback, box, done))
    if not done.wait(timeout):
        raise TimeoutError("La MASTER non ha risposto in tempo")
    ok, value = box.get()
    if not ok:
        raise value
    return value


def _process_import_ui_requests():
    """Esegue sul thread Tk gli aggiornamenti prodotti dal download set."""
    try:
        while True:
            callback = import_ui_requests.get_nowait()
            callback()
    except queue.Empty:
        pass
    root.after(25, _process_import_ui_requests)


def _process_master_ui_requests():
    """Consuma sul thread Tk le richieste ricevute dai thread HTTP."""
    try:
        while True:
            callback, box, done = master_ui_requests.get_nowait()
            try:
                box.put((True, callback()))
            except Exception as exc:
                box.put((False, exc))
            finally:
                done.set()
    except queue.Empty:
        pass
    root.after(25, _process_master_ui_requests)


def _master_snapshot(route, query):
    def build():
        global master_iphone_last_seen
        if route == "status":
            used = sum(v.get("used", 0) for s in sets.values() for v in s.values())
            total = sum(v.get("total", 0) for s in sets.values() for v in s.values())
            online = master_iphone_last_seen is not None and time.time() - master_iphone_last_seen < 4
            return {"ready": bool(sets), "version": version, "sets": len(sets),
                    "used": used, "total": total, "remaining": max(total - used, 0),
                    "iphone_connected": online,
                    "a4_status": dict(master_iphone_a4_status)}

        if route == "commands":
            master_iphone_last_seen = time.time()
            return {"commands": list(master_remote_commands[:5]),
                    "server_time": datetime.now().isoformat(timespec="seconds")}

        if route == "sets":
            rows = []
            for name, parts in sets.items():
                used = sum(v.get("used", 0) for v in parts.values())
                total = sum(v.get("total", 0) for v in parts.values())
                rows.append({"name": name, "code": set_codes.get(name, ""),
                             "number": numerazione_set.get(name), "used": used,
                             "total": total, "remaining": max(total - used, 0)})
            rows.sort(key=lambda row: row["number"] if row["number"] is not None else 999999)
            return {"sets": rows}

        search = str(query.get("query", "")).strip().lower()
        set_filter = str(query.get("set", "")).strip()
        missing_only = str(query.get("missing_only", "0")).lower() in ("1", "true", "yes")
        completed_only = str(query.get("completed_only", "0")).lower() in ("1", "true", "yes")
        rows = []
        source_names = [set_filter] if set_filter in sets else list(sets)
        for name in source_names:
            for key, part in sets[name].items():
                used, total = int(part.get("used", 0)), int(part.get("total", 0))
                if missing_only and used >= total:
                    continue
                if completed_only and used <= 0:
                    continue
                if search and search not in key.lower() and search not in str(part.get("name", "")).lower():
                    continue
                rows.append({"key": key, "part_num": key.split("_", 1)[0],
                             "name": part.get("name", ""), "image": part.get("img", ""),
                             "set": name, "set_number": numerazione_set.get(name),
                             "used": used, "total": total, "remaining": max(total - used, 0),
                             "completed": total > 0 and used >= total})
        return {"pieces": rows, "count": len(rows)}

    return _master_on_ui_thread(build)


def _master_refresh_after_change(key):
    aggiorna_global()
    save_lego_data(force=True)
    save_log(force=True)
    aggiorna_pezzo_griglia(key)
    aggiorna_log_ui()
    aggiorna_riepilogo()
    aggiorna_titolo()
    aggiorna_filtri_set()
    aggiorna_label_movimento()
    if aggiorna_lista_func:
        aggiorna_lista_func()


def invia_comando_iphone(command):
    """Accoda un comando che l'iPhone ritira tramite polling LAN."""
    global master_next_command_id
    if master_server is None:
        messagebox.showerror("Comando iPhone", "La MASTER non è attiva.")
        return
    item = {"id": master_next_command_id, "command": command,
            "created_at": datetime.now().isoformat(timespec="seconds")}
    master_next_command_id += 1
    master_remote_commands.append(item)
    labels = {
        "calibrate": "Calibrazione piano richiesta",
        "calibrate_reference": "Calibrazione Plate richiesta",
        "a4_start": "Guida A4 aperta su iPhone",
        "a4_plane": "Acquisizione piano A4 richiesta",
        "a4_reference": "Verifica Plate A4 richiesta",
        "a4_status": "Lettura stato calibrazione richiesta",
        "a4_close": "Chiusura guida A4 richiesta",
        "analyze": "Analisi richiesta",
    }
    testo = labels.get(command, f"Comando {command}")
    risultato.config(text=f"{testo}: attendi l’iPhone…", fg="#1565c0")


def apri_calibrazione_iphone_a4():
    """Pannello MASTER per la calibrazione A4 di LEGO Vision v13.1 B17."""
    win = tk.Toplevel(root)
    win.title("Calibrazione iPhone A4 — build 17")
    win.geometry("680x680")
    win.transient(root)

    tk.Label(
        win,
        text="Calibrazione guidata iPhone — foglio A4",
        font=("Arial", 18, "bold"),
        fg="black",
    ).pack(pady=(18, 6))
    stato = tk.Label(
        win,
        text="Verifico collegamento iPhone…",
        font=("Arial", 12),
        fg="black",
        wraplength=610,
        justify="center",
    )
    stato.pack(pady=(0, 14))

    def send(command, text):
        invia_comando_iphone(command)
        stato.config(text=text, fg="black")

    steps = tk.Frame(win)
    steps.pack(fill="both", expand=True, padx=22)
    rows = [
        ("1", "Apri guida A4 su iPhone", "a4_start",
         "Apri la guida sull’iPhone e inquadra tutto il foglio A4 con i quattro marker.", "#006cb7"),
        ("2", "Calibra piano vuoto", "a4_plane",
         "Lascia il foglio vuoto e fermo mentre l’iPhone acquisisce il piano.", "#f47b20"),
        ("3", "Avvia Plate rossa 2×4", "a4_reference",
         "Metti la Plate rossa 2×4 al centro del foglio e avvia la verifica.", "#e3000b"),
        ("4", "Leggi stato calibrazione iPhone", "a4_status",
         "Richiedo lo stato attuale di piano, Plate 2×4 e calibrazione A4.", "#aeb8bf"),
        ("5", "Chiudi guida e torna al riconoscimento", "a4_close",
         "La guida viene chiusa e l’iPhone torna al riconoscimento.", "#00a650"),
    ]
    for number, label, command, message, color in rows:
        row = tk.Frame(steps)
        row.pack(fill="x", pady=7)
        tk.Label(row, text=number, width=3, font=("Arial", 16, "bold"),
                 fg="black").pack(side="left")
        button = tk.Button(row, text=label, command=lambda c=command, m=message: send(c, m))
        stile_pulsante(button, color, "#111111", color, bold=True, padx=12, pady=9)
        button.pack(side="left", fill="x", expand=True)

    tk.Label(
        win,
        text=("Controlla la barra da 50 mm sul foglio stampato. Durante i passaggi "
              "2 e 3 non muovere l’iPhone né il foglio A4."),
        font=("Arial", 11),
        fg="black",
        wraplength=610,
        justify="left",
    ).pack(padx=24, pady=12)

    def refresh_status():
        if not win.winfo_exists():
            return
        online = master_iphone_last_seen is not None and time.time() - master_iphone_last_seen < 4
        if online:
            a4 = master_iphone_a4_status
            plane = "OK" if a4.get("plane") else "—"
            reference = "OK" if a4.get("reference") else "—"
            detail = str(a4.get("message", "")).strip()
            stato.config(
                text=f"● iPhone B17 collegato — Piano: {plane}  Plate 2×4: {reference}\n{detail}",
                fg="black",
            )
        elif master_server is None:
            stato.config(text="MASTER non attiva", fg="black")
        else:
            stato.config(text="Attendo LEGO Vision v13.1 build 17 sulla rete locale…", fg="black")
        win.after(1200, refresh_status)

    refresh_status()


def _camera_mode_label(mode=None):
    value = mode or camera_config.get("mode", "iphone")
    return {"iphone": "iPhone", "camera1": "1 fotocamera", "camera2": "2 fotocamere"}.get(value, "iPhone")


def _close_camera_session():
    global camera_session
    if camera_session is not None:
        try:
            camera_session.close()
        except Exception:
            pass
        camera_session = None


def _ensure_camera_session():
    global camera_session
    mode = camera_config.get("mode", "iphone")
    if mode == "iphone":
        raise CameraError("La sorgente attiva è iPhone.")
    count = 2 if mode == "camera2" else 1
    indices = list(camera_config.get("indices", [0, 1]))[:count]
    if len(indices) != count:
        raise CameraError("Configura prima le fotocamere da Sorgente visiva.")
    if camera_session is None or camera_session.indices != indices:
        _close_camera_session()
        camera_session = CameraSession(indices, camera_calibration_dir)
        camera_session.open()
    return camera_session


def _color_adjustment(detected, candidate):
    detected = str(detected or "Unknown").lower()
    candidate = str(candidate or "Unknown").lower()
    if detected == "unknown":
        return 0
    if detected == candidate:
        return 8
    grays = {"dark bluish gray", "light bluish gray", "gray", "dark gray"}
    if detected in grays and candidate in grays:
        return 3
    return -8


def _webcam_candidates(predictions, detected_color):
    """Incrocia Brickognize con tutte le destinazioni ancora mancanti."""
    rows = []
    seen = set()
    for prediction in predictions:
        predicted_id = str(prediction.get("id", ""))
        score = float(prediction.get("score", 0.0))
        for set_name, parts in sets.items():
            for key, part in parts.items():
                used, total = int(part.get("used", 0)), int(part.get("total", 0))
                if total <= 0 or used >= total:
                    continue
                part_num = key.split("_", 1)[0]
                if not ids_match(part_num, predicted_id):
                    continue
                unique = (set_name, key)
                if unique in seen:
                    continue
                seen.add(unique)
                color = key.split("_", 1)[1] if "_" in key else "Unknown"
                confidence = max(1, min(99, int(round(score * 100)) + _color_adjustment(detected_color, color)))
                rows.append({
                    "piece_key": key,
                    "name": prediction.get("name") or part.get("name", ""),
                    "image": prediction.get("img_url") or part.get("img", ""),
                    "color": color,
                    "set_name": set_name,
                    "set_number": numerazione_set.get(set_name),
                    "used": used,
                    "total": total,
                    "score": round(score, 4),
                    "confidence": confidence,
                    "views": int(prediction.get("views", 1)),
                })
    rows.sort(key=lambda row: (-row["confidence"], row["set_number"] or 999999, row["piece_key"]))
    return rows[:8]


def calibra_sorgente_visiva():
    if camera_config.get("mode") == "iphone":
        apri_calibrazione_iphone_a4()
        return
    risultato.config(text="Calibrazione: lascia il piano vuoto…", fg="#ef6c00")

    def worker():
        try:
            session = _ensure_camera_session()
            session.calibrate_plane()
            root.after(0, lambda: risultato.config(
                text=f"Piano salvato per {_camera_mode_label()}. Ora metti un pezzo al centro.", fg="#2e7d32"))
        except Exception as exc:
            root.after(0, lambda e=str(exc): messagebox.showerror("Calibrazione fotocamere", e))
    threading.Thread(target=worker, daemon=True).start()


def analizza_sorgente_visiva():
    if camera_config.get("mode") == "iphone":
        invia_comando_iphone("analyze")
        return
    risultato.config(text=f"Analisi con {_camera_mode_label()}…", fg="#1565c0")

    def progress(message):
        root.after(0, lambda m=message: risultato.config(text=m, fg="#1565c0"))

    def worker():
        try:
            session = _ensure_camera_session()
            if not all(session.has_plane(index) for index in session.indices):
                raise CameraError("Prima calibra il piano vuoto per tutte le fotocamere.")
            predictions, frames = session.recognize(progress)
            colors = [frame.color_name for frame in frames if frame.color_name != "Unknown"]
            detected_color = max(set(colors), key=colors.count) if colors else "Unknown"
            candidates = _webcam_candidates(predictions, detected_color)
            measurement = {
                "width_mm": 0.0, "length_mm": 0.0, "height_mm": 0.0,
                "color": detected_color, "source": _camera_mode_label(),
                "views": len(frames), "recognized_id": predictions[0].get("id", "") if predictions else "",
            }
            root.after(0, lambda: mostra_candidati_master(candidates, measurement))
            root.after(0, lambda: risultato.config(
                text=(f"{_camera_mode_label()}: {len(candidates)} candidati mancanti"
                      if candidates else "Pezzo riconosciuto, ma non trovato tra quelli mancanti"),
                fg="#2e7d32" if candidates else "#ef6c00"))
        except Exception as exc:
            root.after(0, lambda e=str(exc): messagebox.showerror("Analisi fotocamere", e))
            root.after(0, lambda: risultato.config(text="Analisi fotocamere non riuscita", fg="#c62828"))
    threading.Thread(target=worker, daemon=True).start()


def apri_sorgente_visiva():
    global camera_window
    if camera_window is not None and camera_window.winfo_exists():
        camera_window.lift()
        return

    win = tk.Toplevel(root)
    camera_window = win
    win.title("Sorgente visiva LEGO")
    win.geometry("940x720")
    win.transient(root)
    mode_var = tk.StringVar(value=camera_config.get("mode", "iphone"))
    indices = list(camera_config.get("indices", [0, 1])) + [0, 1]
    camera_a_var = tk.StringVar()
    camera_b_var = tk.StringVar()
    available_cameras = {"choices": [], "by_label": {}}
    status = tk.Label(win, text="Scegli come acquisire il pezzo.", font=("Arial", 12), wraplength=850)
    preview_running = threading.Event()
    preview_session = {"camera": None}

    tk.Label(win, text="Sorgente visiva", font=("Arial", 21, "bold")).pack(pady=(18, 6))
    modes = tk.Frame(win)
    modes.pack(fill="x", padx=22, pady=8)
    for value, label, detail in (
        ("iphone", "iPhone", "Usa LEGO Vision e la calibrazione A4 esistente."),
        ("camera1", "1 fotocamera", "Una webcam riprende sagoma, colore e dettagli."),
        ("camera2", "2 fotocamere", "Vista superiore + inclinata, risultati fusi."),
    ):
        card = tk.Frame(modes, bd=1, relief="solid", padx=9, pady=8)
        card.pack(side="left", fill="both", expand=True, padx=5)
        tk.Radiobutton(card, text=label, variable=mode_var, value=value,
                       font=("Arial", 14, "bold")).pack(anchor="w")
        tk.Label(card, text=detail, justify="left", wraplength=240).pack(anchor="w", pady=(3, 0))

    selectors = tk.LabelFrame(win, text="Fotocamere USB", padx=12, pady=10)
    selectors.pack(fill="x", padx=27, pady=10)
    tk.Label(selectors, text="Vista principale (dall’alto):").grid(row=0, column=0, sticky="w", padx=6, pady=5)
    menu_a = ttk.Combobox(selectors, textvariable=camera_a_var, state="readonly", width=52)
    menu_a.grid(row=0, column=1, sticky="w")
    tk.Label(selectors, text="Seconda vista (inclinata):").grid(row=1, column=0, sticky="w", padx=6, pady=5)
    menu_b = ttk.Combobox(selectors, textvariable=camera_b_var, state="readonly", width=52)
    menu_b.grid(row=1, column=1, sticky="w")

    preview_frame = tk.Frame(win, bg="#242424", height=340)
    preview_frame.pack(fill="both", expand=True, padx=27, pady=8)
    preview_a = tk.Label(preview_frame, text="Anteprima principale", bg="#242424", fg="white")
    preview_b = tk.Label(preview_frame, text="Anteprima inclinata", bg="#242424", fg="white")
    preview_a.pack(side="left", fill="both", expand=True, padx=3, pady=3)
    preview_b.pack(side="left", fill="both", expand=True, padx=3, pady=3)
    status.pack(pady=4)

    def selected_indices():
        labels = [camera_a_var.get(), camera_b_var.get()] if mode_var.get() == "camera2" else [camera_a_var.get()]
        try:
            return [available_cameras["by_label"][label] for label in labels]
        except KeyError:
            raise CameraError("Premi Cerca fotocamere e scegli le camere disponibili.")

    def update_camera_choices(found):
        labels = [choice.label for choice in found]
        available_cameras["choices"] = found
        available_cameras["by_label"] = {choice.label: choice.index for choice in found}
        menu_a["values"] = labels
        menu_b["values"] = labels
        preferred_a, preferred_b = indices[0], indices[1]
        if labels:
            camera_a_var.set(next((c.label for c in found if c.index == preferred_a), labels[0]))
            fallback_b = labels[1] if len(labels) > 1 else labels[0]
            camera_b_var.set(next((c.label for c in found if c.index == preferred_b), fallback_b))
        else:
            camera_a_var.set("")
            camera_b_var.set("")

    def show_frames(frames):
        for label, frame in zip((preview_a, preview_b), frames):
            rgb = frame.cropped_bgr[:, :, ::-1]
            image = Image.fromarray(rgb)
            image.thumbnail((420, 330), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(image)
            label.config(image=photo, text="")
            label.image = photo
        if len(frames) == 1:
            preview_b.config(image="", text="Seconda vista non attiva")
            preview_b.image = None

    def stop_preview():
        preview_running.clear()
        session = preview_session.pop("camera", None)
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        preview_session["camera"] = None

    def preview_worker():
        stop_preview()
        preview_running.set()
        try:
            if mode_var.get() == "iphone":
                if not master_pairing_info:
                    raise CameraError("MASTER non attiva: avviala prima dell'anteprima iPhone.")
                url = master_pairing_info["address"].rstrip("/") + "/api/preview"
                headers = {"X-Master-PIN": master_pairing_info["pin"]}
                root.after(0, lambda: status.config(text="Attendo i frame da LEGO Vision iPhone…", fg="black"))
                while preview_running.is_set():
                    response = requests.get(url, headers=headers, timeout=2)
                    if response.status_code == 404:
                        time.sleep(0.25)
                        continue
                    response.raise_for_status()
                    image = Image.open(BytesIO(response.content)).convert("RGB")
                    image.thumbnail((840, 500), Image.Resampling.LANCZOS)
                    root.after(0, lambda img=image.copy(): show_iphone_frame(img))
                    time.sleep(0.12)
            else:
                temp = CameraSession(selected_indices(), camera_calibration_dir)
                preview_session["camera"] = temp
                temp.open()
                root.after(0, lambda: status.config(text="Anteprima webcam live attiva.", fg="black"))
                while preview_running.is_set():
                    frames = temp.preview()
                    root.after(0, lambda rows=frames: show_frames(rows))
                    time.sleep(0.08)
        except Exception as exc:
            if preview_running.is_set():
                root.after(0, lambda e=str(exc): status.config(text=e, fg="#c62828"))
        finally:
            stop_preview()

    def show_iphone_frame(image):
        photo = ImageTk.PhotoImage(image)
        preview_a.config(image=photo, text="")
        preview_a.image = photo
        preview_b.config(image="", text="Streaming dalla fotocamera posteriore iPhone")
        preview_b.image = None

    def probe_worker():
        root.after(0, lambda: status.config(text="Cerco le fotocamere collegate…", fg="black"))
        try:
            found = discover_cameras()
            text = ", ".join(choice.label for choice in found) or "Nessuna fotocamera rilevata"
            root.after(0, lambda rows=found: update_camera_choices(rows))
            root.after(0, lambda t=text: status.config(text=t, fg="black"))
        except Exception as exc:
            root.after(0, lambda e=str(exc): status.config(text=e, fg="#c62828"))

    def save():
        global camera_config
        chosen = selected_indices()
        if mode_var.get() == "camera2" and len(set(chosen)) != 2:
            messagebox.showwarning("Due fotocamere", "Scegli due indici diversi.")
            return
        _close_camera_session()
        camera_config = {"mode": mode_var.get(), "indices": chosen}
        save_camera_config(camera_config_file, camera_config["mode"], chosen)
        aggiorna_controlli_sorgente()
        risultato.config(text=f"Sorgente attiva: {_camera_mode_label()}", fg="#1565c0")
        close_window()

    def close_window():
        global camera_window
        stop_preview()
        camera_window = None
        win.destroy()

    buttons = tk.Frame(win)
    buttons.pack(fill="x", padx=27, pady=(4, 16))
    tk.Button(buttons, text="Cerca fotocamere", command=lambda: threading.Thread(target=probe_worker, daemon=True).start()).pack(side="left", padx=4)
    tk.Button(buttons, text="Avvia anteprima live", command=lambda: threading.Thread(target=preview_worker, daemon=True).start()).pack(side="left", padx=4)
    tk.Button(buttons, text="Ferma anteprima", command=stop_preview).pack(side="left", padx=4)
    tk.Button(buttons, text="Salva sorgente", command=save, bg="#00a650").pack(side="right", padx=4)
    tk.Button(buttons, text="Annulla", command=close_window).pack(side="right", padx=4)
    win.protocol("WM_DELETE_WINDOW", close_window)
    threading.Thread(target=probe_worker, daemon=True).start()


def mostra_candidati_master(candidates, measurement):
    """Mostra sul Mac risultati provenienti da iPhone o webcam."""
    win = tk.Toplevel(root)
    source = measurement.get("source", "iPhone")
    win.title(f"Candidati riconosciuti — {source}")
    win.geometry("980x720")
    if source == "iPhone":
        testo_misure = (f"Misura: {measurement.get('width_mm', 0):.1f} × "
                        f"{measurement.get('length_mm', 0):.1f} × "
                        f"{measurement.get('height_mm', 0):.1f} mm — "
                        f"{measurement.get('color', 'Unknown')}")
    else:
        testo_misure = (f"{source} — colore {measurement.get('color', 'Unknown')} — "
                        f"{measurement.get('views', 1)} vista/e — "
                        f"Brickognize {measurement.get('recognized_id', '—') or '—'}")
    tk.Label(win, text=testo_misure, font=("Arial", 15, "bold")).pack(pady=12)
    body = tk.Frame(win)
    body.pack(fill="both", expand=True, padx=12, pady=4)
    if not candidates:
        tk.Label(body, text="Nessun candidato trovato", font=("Arial", 16)).pack(pady=30)
        return

    def conferma(row):
        click_pezzo_generic(row["set_name"], row["piece_key"], 1)
        win.destroy()

    def carica_foto(label, row):
        """Scarica/elabora fuori dal thread Tk e crea PhotoImage sul thread UI."""
        def worker():
            try:
                path = get_image_path(row["piece_key"])
                if not os.path.exists(path) and row.get("image"):
                    response = requests.get(row["image"], timeout=7)
                    response.raise_for_status()
                    image = Image.open(BytesIO(response.content)).convert("RGBA")
                    image.save(path)
                else:
                    image = Image.open(path).convert("RGBA")
                image.thumbnail((112, 112), Image.Resampling.LANCZOS)

                def update():
                    try:
                        if label.winfo_exists():
                            photo = ImageTk.PhotoImage(image)
                            label.config(image=photo, text="", width=112, height=112)
                            label.image = photo
                    except Exception:
                        pass

                master_ui_requests.put((update, queue.Queue(maxsize=1), threading.Event()))
            except Exception:
                def failed():
                    try:
                        if label.winfo_exists():
                            label.config(text="Foto\nnon disponibile")
                    except Exception:
                        pass
                master_ui_requests.put((failed, queue.Queue(maxsize=1), threading.Event()))
        threading.Thread(target=worker, daemon=True).start()

    for index, row in enumerate(candidates, 1):
        card = tk.Frame(body, bd=1, relief="solid", padx=10, pady=7)
        card.pack(fill="x", pady=4)
        image_label = tk.Label(card, text="Caricamento…", width=15, height=6,
                               bg="white", relief="groove")
        image_label.pack(side="left", padx=(0, 12))
        descrizione = (f"{index}. {row['name']}  [{row['piece_key']}]\n"
                       f"{row.get('set_number', '?')}. {row['set_name']}   "
                       f"compatibilità {row['confidence']}%   "
                       f"{row['used']}/{row['total']}")
        tk.Label(card, text=descrizione, justify="left", anchor="w",
                 font=("Arial", 13)).pack(side="left", fill="x", expand=True)
        tk.Button(card, text="È questo: aggiungi", font=("Arial", 12, "bold"),
                  command=lambda r=row: conferma(r), bg="#2e7d32", fg="white").pack(side="right", padx=6)
        carica_foto(image_label, row)


def _master_recognize(payload):
    """Ordina i soli pezzi mancanti usando colore e misure TrueDepth."""
    try:
        observed_w = max(1.0, float(payload.get("width_mm", 0)))
        observed_l = max(1.0, float(payload.get("length_mm", 0)))
        observed_h = max(0.5, float(payload.get("height_mm", 0)))
    except Exception:
        return {"ok": False, "error": "Misure non valide", "candidates": []}
    observed_color = str(payload.get("color", "Unknown")).strip()
    obs_min, obs_max = sorted((observed_w, observed_l))

    def color_penalty(candidate):
        if observed_color == "Unknown":
            return 0.35
        if candidate.lower() == observed_color.lower():
            return 0.0
        gray = {"dark bluish gray", "light bluish gray", "gray", "dark gray"}
        if candidate.lower() in gray and observed_color.lower() in gray:
            return 0.18
        if candidate.lower().startswith("trans-") and candidate.lower().endswith(observed_color.lower()):
            return 0.35
        return 1.45

    def expected_height(part_name):
        text = part_name.lower()
        if "tile" in text or "plate" in text:
            return 3.2
        if "brick" in text:
            return 9.6
        if "slope" in text:
            return 7.0
        return None

    ranked = []
    for set_name, parts in sets.items():
        for key, part in parts.items():
            used, total = int(part.get("used", 0)), int(part.get("total", 0))
            if total <= 0 or used >= total:
                continue
            name = str(part.get("name", ""))
            color = key.split("_", 1)[1] if "_" in key else "Unknown"
            score = color_penalty(color)

            footprint = re.search(r"(?<!\d)(\d+)\s*x\s*(\d+)(?!\d)", name, re.IGNORECASE)
            if footprint:
                exp_a = max(1, int(footprint.group(1))) * 8.0
                exp_b = max(1, int(footprint.group(2))) * 8.0
                exp_min, exp_max = sorted((exp_a, exp_b))
                score += min(2.5, abs(obs_min - exp_min) / max(exp_min, 8.0)
                             + abs(obs_max - exp_max) / max(exp_max, 8.0))
            else:
                score += 0.95

            exp_h = expected_height(name)
            if exp_h is not None:
                score += min(1.4, abs(observed_h - exp_h) / max(exp_h, 3.2))
            else:
                score += 0.35

            confidence = max(1, min(99, int(round(100 * (1.0 - min(score, 4.0) / 4.0)))))
            ranked.append({
                "piece_key": key, "name": name, "image": part.get("img", ""),
                "color": color, "set_name": set_name,
                "set_number": numerazione_set.get(set_name), "used": used, "total": total,
                "score": round(score, 4), "confidence": confidence
            })

    ranked.sort(key=lambda row: (row["score"], row["set_number"] or 999999,
                                 row["piece_key"]))
    result = {"ok": True, "measurement": {"width_mm": observed_w,
            "length_mm": observed_l, "height_mm": observed_h, "color": observed_color},
            "candidates": ranked[:5], "searched_missing": len(ranked)}
    root.after(0, lambda: mostra_candidati_master(result["candidates"], result["measurement"]))
    return result


def _master_action(payload):
    def apply():
        global master_remote_commands, master_iphone_a4_status
        action = str(payload.get("action", "")).lower()
        if action == "command_result":
            try:
                command_id = int(payload.get("command_id", 0))
            except Exception:
                command_id = 0
            master_remote_commands = [c for c in master_remote_commands if c["id"] != command_id]
            message = str(payload.get("message", "Comando completato"))
            reported_status = payload.get("a4_status")
            if isinstance(reported_status, dict):
                if "plane" in reported_status:
                    master_iphone_a4_status["plane"] = bool(reported_status["plane"])
                if "reference" in reported_status:
                    master_iphone_a4_status["reference"] = bool(reported_status["reference"])
                if reported_status.get("message"):
                    message = str(reported_status["message"])

            lower = message.lower()
            if "piano=ok" in lower or "piano ok" in lower or "piano calibrato" in lower:
                master_iphone_a4_status["plane"] = True
            elif "piano=no" in lower or "piano no" in lower:
                master_iphone_a4_status["plane"] = False
            if "plate=ok" in lower or "plate ok" in lower or (
                "plate 2×4" in lower and ("complet" in lower or "riconosci" in lower)
            ):
                master_iphone_a4_status["reference"] = True
            elif "plate=no" in lower or "plate no" in lower:
                master_iphone_a4_status["reference"] = False
            if any(token in lower for token in ("piano", "plate", "calibrazione a4")):
                master_iphone_a4_status["message"] = message

            risultato.config(text=f"iPhone B17: {message}", fg="#2e7d32")
            return {"ok": True, "command_id": command_id,
                    "a4_status": dict(master_iphone_a4_status)}
        if action == "recognize":
            return _master_recognize(payload)
        key = str(payload.get("piece_key", "")).strip()
        set_name = str(payload.get("set_name", "")).strip()
        try:
            quantity = max(1, min(99, int(payload.get("quantity", 1))))
        except Exception:
            quantity = 1
        if action not in ("add", "remove"):
            return {"ok": False, "error": "azione non valida"}
        if not key:
            return {"ok": False, "error": "piece_key mancante"}

        if set_name:
            candidates = [set_name] if set_name in sets and key in sets[set_name] else []
        elif action == "add":
            candidates = get_available_set_names_for_piece(key)
        else:
            candidates = [name for name, parts in sets.items()
                          if key in parts and parts[key].get("used", 0) > 0]
        if not candidates:
            return {"ok": False, "error": "Pezzo/set non disponibile"}

        name = candidates[0]
        part = sets[name][key]
        before = int(part.get("used", 0))
        total = int(part.get("total", 0))
        after = min(total, before + quantity) if action == "add" else max(0, before - quantity)
        changed = abs(after - before)
        if changed == 0:
            return {"ok": False, "error": "Quantità già al limite", "used": before, "total": total}
        part["used"] = after

        global ultimo_movimento, ultimo_set_modificato
        ultimo_set_modificato = name
        number = numerazione_set.get(name, "?")
        if action == "add":
            log_pezzi.append({"event": "piece_click", "source": "iphone", "key": key,
                              "set": name, "used": after, "total": total, "count": changed,
                              "timestamp": datetime.now().isoformat(timespec="seconds")})
            ultimo_movimento = f"iPhone → {number}. {name} ({after}/{total})"
        else:
            log_pezzi.append({"event": "undo", "source": "iphone", "key": key,
                              "set": name, "used": after, "total": total, "count": changed,
                              "timestamp": datetime.now().isoformat(timespec="seconds")})
            ultimo_movimento = f"iPhone toglie → {number}. {name} ({after}/{total})"
        _master_refresh_after_change(key)
        return {"ok": True, "action": action, "piece_key": key, "set_name": name,
                "set_number": number, "changed": changed, "used": after,
                "total": total, "remaining": max(total - after, 0),
                "completed": total > 0 and after >= total}

    return _master_on_ui_thread(apply)


def start_master_server():
    global master_server, master_pairing_info, preview_transport
    config = load_master_config()
    if not config.get("enabled", True):
        return
    try:
        from master_server import MasterServer
        master_server = MasterServer(_master_snapshot, _master_action, config["pin"], config["port"])
        address = master_server.start()
        master_pairing_info = {"address": address, "pin": config["pin"]}
        try:
            from preview_transport import PreviewTransport
            preview_transport = PreviewTransport(
                config["pin"], config["port"], master_server.set_preview)
            ws_port = preview_transport.start()
            master_pairing_info["ws_port"] = ws_port
        except Exception as exc:
            preview_transport = None
            print(f"[PREVIEW] Trasporto stabile non avviato: {exc}")
        print(f"[MASTER] Attiva su {address}  PIN {config['pin']}")
        root.title(f"LEGO Smista PRO - MASTER  {address}  PIN {config['pin']}")
    except Exception as exc:
        master_server = None
        master_pairing_info = None
        print(f"[MASTER] Avvio non riuscito: {exc}")


def mostra_qr_master():
    """Mostra il QR locale per configurare automaticamente l'app iPhone."""
    if not master_pairing_info:
        messagebox.showerror("QR MASTER", "La MASTER non è attiva.")
        return
    try:
        import qrcode
    except ImportError:
        messagebox.showerror(
            "Modulo QR mancante",
            "Installa una sola volta il modulo QR dal Terminale:\n\n"
            "python3 -m pip install qrcode[pil]"
        )
        return

    from urllib.parse import urlencode
    payload = "legomaster://pair?" + urlencode(master_pairing_info)
    image = qrcode.make(payload).convert("RGB").resize((420, 420), Image.Resampling.NEAREST)
    photo = ImageTk.PhotoImage(image)

    win = tk.Toplevel(root)
    win.title("Configura iPhone con QR")
    win.resizable(False, False)
    tk.Label(win, text="Inquadra con TrueDepth LEGO", font=("Arial", 18, "bold")).pack(pady=(16, 8))
    qr_label = tk.Label(win, image=photo, bd=8, relief="solid")
    qr_label.image = photo
    qr_label.pack(padx=18, pady=8)
    tk.Label(
        win,
        text=f"{master_pairing_info['address']}\nPIN {master_pairing_info['pin']}",
        font=("Arial", 13), justify="center"
    ).pack(pady=(6, 16))

def inserisci_quantita(k):
    colore_pezzo = k.split("_")[1] if "_" in k else "Tutti"

    def filtra_per_colore_pezzo():
        if is_trans_color(colore_pezzo) and "Trans" in colori_disponibili:
            target_colore = "Trans"
        else:
            target_colore = colore_pezzo if colore_pezzo in colori_disponibili else "Tutti"
        color_var.set(target_colore)
        on_color_filter_change(target_colore)

    def costruisci_ui():
        # Pulisce la finestra
        for w in frame_inner.winfo_children():
            w.destroy()

        entries.clear()
        selected_set = get_selected_set_name()

        disponibili = []

        # Click destro: mostra tutti i set che hanno il pezzo, anche con filtro set attivo
        for nome in get_all_set_names_for_piece(k):
            s = sets[nome]
            if k in s:
                v = s[k]
                numero = numerazione_set.get(nome, "?")
                completo = v["used"] >= v["total"]
                disponibili.append((numero, nome, v, completo))

        if not disponibili:
            tk.Label(frame_inner, text="Nessun set disponibile").pack()
            return

        # Layout a colonne reali: niente width fissa in caratteri, allineamento pulito
        frame_inner.grid_columnconfigure(0, weight=1)
        frame_inner.grid_columnconfigure(1, weight=0)
        frame_inner.grid_columnconfigure(2, weight=0)

        for riga, (numero, nome, v, completo) in enumerate(disponibili):
            max_add = v["total"] - v["used"]
            label_font = ("Arial", 13, "bold") if selected_set and nome == selected_set else ("Arial", 13)

            if completo:
                label_text = f"{numero}. {nome} ({v['used']}/{v['total']})"
            else:
                label_text = f"{numero}. {nome} ({v['used']}/{v['total']}) max:{max_add}"

            lbl_set_row = tk.Label(
                frame_inner,
                text=label_text,
                anchor="w",
                font=label_font,
                cursor="hand2"
            )
            lbl_set_row.grid(row=riga, column=0, sticky="w", padx=(5, 8), pady=2)
            lbl_set_row.bind("<Button-1>", lambda _e, n=nome: apri_dettaglio_set_esistente(n))

            if completo:
                lbl_set_row.config(fg="#666666")
                tk.Label(
                    frame_inner,
                    text="completo",
                    fg="#2e7d32",
                    font=("Arial", 12, "bold"),
                    anchor="w"
                ).grid(row=riga, column=1, columnspan=2, sticky="w", padx=(0, 6), pady=2)
                continue

            entry = tk.Entry(frame_inner, width=6, justify="center", font=("Arial", 13))
            entry.grid(row=riga, column=1, padx=(0, 6), pady=2)

            # Bottone Max
            def inserisci_max(e=entry, val=max_add):
                e.delete(0, tk.END)
                e.insert(0, str(val))

            tk.Button(frame_inner, text="Max", width=7, font=("Arial", 13, "bold"), command=inserisci_max).grid(
                row=riga, column=2, padx=(0, 5), pady=2
            )

            entries[nome] = (entry, max_add)

        # Aggiorna larghezza/altezza finestra automaticamente in base al contenuto
        win.update_idletasks()
        larghezza = min(max(520, frame_inner.winfo_reqwidth() + scrollbar.winfo_reqwidth() + 30), 1500)
        altezza = min(max(150, frame_inner.winfo_height() + 80), 650)
        win.geometry(f"{larghezza}x{altezza}")

    def conferma():
        modificato = False

        for nome, (entry, max_add) in entries.items():
            try:
                qty = int(entry.get())
            except:
                continue

            if qty <= 0:
                continue

            qty = min(qty, max_add)

            # Incremento tutto in un colpo solo
            sets[nome][k]["used"] += qty
            v = sets[nome][k]
            numero = numerazione_set.get(nome, "?")

            global ultimo_movimento, ultimo_set_modificato
            ultimo_movimento = f"Ultimo pezzo → {numero}. {nome} ({v['used']}/{v['total']})"
            ultimo_set_modificato = nome

            # Log singolo con valore finale
            log_pezzi.append({
                "event": "piece_click",
                "key": k,
                "set": nome,
                "used": v["used"],
                "total": v["total"],
                "count": qty   # <-- quantità effettiva inserita
            })
            print(
                f"[LOGDBG] append piece_click_batch: key={k} set={nome} "
                f"count={qty} used={v['used']}/{v['total']}"
            )

            modificato = True

        if not modificato:
            return

        # Salvataggi e aggiornamenti
        save_log()
        aggiorna_global()
        save_lego_data(force=True)
        aggiorna_log_ui()
        aggiorna_riepilogo()
        aggiorna_filtri_set()
        aggiorna_titolo()
        if aggiorna_lista_func:
            aggiorna_lista_func()
        aggiorna_label_movimento()
        aggiorna_pezzo_griglia(k)
        aggiorna_griglia(force=True)
        # Ricostruisce UI aggiornata
        costruisci_ui()

    # ------------------------
    # UI finestra inserimento
    # ------------------------
    win = tk.Toplevel(root)
    win.withdraw()
    win.title("Inserisci quantità per set")

    # Frame pulsanti sopra
    frame_top = tk.Frame(win)
    frame_top.pack(fill="x", pady=5)

    tk.Button(frame_top, text="OK", font=("Arial", 13, "bold"), command=conferma).pack(side="left", padx=10)
    tk.Button(frame_top, text="Chiudi", font=("Arial", 13, "bold"), command=win.destroy).pack(side="left", padx=5)
    tk.Button(
        frame_top,
        text=f"Filtra colore: {colore_pezzo}",
        font=("Arial", 12, "bold"),
        command=filtra_per_colore_pezzo
    ).pack(side="left", padx=4)

    # Canvas scrollabile
    canvas = tk.Canvas(win)
    bind_mousewheel_scroll(canvas)
    canvas.pack(side="left", fill="both", expand=True)

    scrollbar = tk.Scrollbar(win, command=canvas.yview)
    scrollbar.pack(side="right", fill="y")

    canvas.configure(yscrollcommand=scrollbar.set)

    frame_inner = tk.Frame(canvas)
    canvas.create_window((0, 0), window=frame_inner, anchor="nw")
    frame_inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

    entries = {}

    costruisci_ui()
    win.deiconify()

# Carica log all'avvio
load_log()

# ------------------------
# CLICK + HOVER
# ------------------------
def _build_hover_feedback(key):
    for nome in get_available_set_names_for_piece(key):
        s = sets[nome]
        if key in s and s[key]["used"] < s[key]["total"]:
            v = s[key]
            numero = numerazione_set.get(nome, "?")
            mancanti = v["total"] - v["used"]
            return f"Metti in: {numero}. {nome} → {v['used']}/{v['total']} (mancano {mancanti})", "green"

    if ultimo_movimento:
        return ultimo_movimento, "orange"
    return "Nessun set disponibile", "red"


def _apply_pending_hover_update():
    global hover_update_job, last_hover_result_text
    hover_update_job = None

    if not pending_hover_key:
        return

    testo, colore = _build_hover_feedback(pending_hover_key)
    if testo != last_hover_result_text:
        risultato.config(text=testo, fg=colore)
        last_hover_result_text = testo


def _apply_hover_leave_clear():
    global hover_clear_job, hover_piece_key
    hover_clear_job = None
    if hover_piece_key is None:
        _set_hover_piece_key(None)


def on_hover_pezzo(event, k, cell_ref=None):
    global hover_update_job, pending_hover_key, hover_clear_job
    if hover_clear_job is not None:
        try:
            root.after_cancel(hover_clear_job)
        except Exception:
            pass
        hover_clear_job = None
    _set_hover_piece_key(k)
    if cell_ref is not None:
        _set_cell_hover_visual(cell_ref, is_hover=True)

    pending_hover_key = k

    # Chiamata programmatica (es. dopo click): aggiorna subito.
    if event is None:
        if hover_update_job is not None:
            try:
                root.after_cancel(hover_update_job)
            except Exception:
                pass
            hover_update_job = None
        _apply_pending_hover_update()
        return

    if hover_update_job is not None:
        try:
            root.after_cancel(hover_update_job)
        except Exception:
            pass
    hover_update_job = root.after(HOVER_UPDATE_DELAY_MS, _apply_pending_hover_update)

def on_leave_pezzo(event, cell_ref=None):
    global hover_update_job, pending_hover_key, hover_clear_job
    pending_hover_key = None
    if hover_update_job is not None:
        try:
            root.after_cancel(hover_update_job)
        except Exception:
            pass
        hover_update_job = None

    # Ritarda leggermente il reset hover: evita lavoro inutile quando si passa subito al pezzo adiacente.
    if hover_clear_job is not None:
        try:
            root.after_cancel(hover_clear_job)
        except Exception:
            pass
    hover_clear_job = root.after(30, _apply_hover_leave_clear)

def aggiorna_label_movimento():
    if ultimo_movimento:
        label_movimento.config(text=ultimo_movimento, fg="green")
    else:
        label_movimento.config(text="Metti in →", fg="black")

def aggiorna_progresso_set():
    """Aggiorna la barra progresso testuale per il set correntemente selezionato"""
    if 'label_progresso_set' not in globals() or label_progresso_set is None:
        return
    try:
        if not label_progresso_set.winfo_exists():
            return
    except Exception:
        return
    selected_name = get_selected_set_name()
    if selected_name and selected_name in sets:
        s = sets[selected_name]
        usati = sum(v["used"] for v in s.values())
        totali = sum(v["total"] for v in s.values())
    else:
        usati = sum(v["used"] for s in sets.values() for v in s.values())
        totali = sum(v["total"] for s in sets.values() for v in s.values())
        selected_name = None
    perc = int(usati / totali * 100) if totali > 0 else 0
    filled = perc // 10
    bar = "█" * filled + "░" * (10 - filled)
    if selected_name:
        numero = numerazione_set.get(selected_name, "?")
        testo = f"{numero}. {selected_name}  [{bar}]  {usati}/{totali}  ({perc}%)"
    else:
        testo = f"Tutti  [{bar}]  {usati}/{totali}  ({perc}%)"
    label_progresso_set.config(text=testo, fg="green" if perc >= 100 else "#333333")

def annulla_ultimo_pezzo(_event=None):
    """Annulla l'ultimo click su un pezzo (Ctrl+Z)"""
    if not log_pezzi:
        print("[LOGDBG] undo: nessun evento nel log")
        return
    item = None
    item_idx = None
    for i in reversed(range(len(log_pezzi))):
        candidate = log_pezzi[i]
        if candidate.get("event") == "set_move":
            continue
        if "key" in candidate and "set" in candidate:
            item = candidate
            item_idx = i
            break

    if item is None:
        print("[LOGDBG] undo: nessun evento pezzo trovato (solo set_move o invalidi)")
        return

    k = item["key"]
    ns = item["set"]
    if ns not in sets or k not in sets[ns]:
        print(f"[LOGDBG] undo: skip per evento non valido key={k} set={ns}")
        return
    item_count = item.get("count", 1)
    item_count -= 1
    sets[ns][k]["used"] = max(sets[ns][k]["used"] - 1, 0)
    if item_count <= 0:
        log_pezzi.pop(item_idx)
    else:
        item["count"] = item_count
        item["used"] = sets[ns][k]["used"]
    print(
        f"[LOGDBG] undo: key={k} set={ns} nuovo_used={sets[ns][k]['used']} "
        f"remaining_count={max(item_count, 0)}"
    )
    global ultimo_movimento, ultimo_set_modificato
    ultimo_movimento = ""
    ultimo_set_modificato = None
    for candidate in reversed(log_pezzi):
        if candidate.get("event") == "set_move":
            continue
        if "key" not in candidate or "set" not in candidate:
            continue
        lk, lns = candidate["key"], candidate["set"]
        if lns in sets and lk in sets[lns]:
            num = numerazione_set.get(lns, "?")
            ultimo_movimento = f"↩ Annullato → {num}. {lns}"
            ultimo_set_modificato = lns
        break
    # Aggiungi evento undo nel log
    log_pezzi.append({
        "event": "undo",
        "key": k,
        "set": ns,
        "used": sets[ns][k]["used"],
        "total": sets[ns][k].get("total", 0),
        "timestamp": datetime.now().isoformat(timespec="seconds")
    })
    save_log()
    aggiorna_global()
    save_lego_data(force=True)
    aggiorna_pezzo_griglia(k)
    aggiorna_log_ui()
    aggiorna_riepilogo()
    aggiorna_titolo()
    aggiorna_filtri_set()
    aggiorna_label_movimento()
    if aggiorna_lista_func:
        aggiorna_lista_func()

# ------------------------
# FILTRI
# ------------------------
colori_disponibili = ["Tutti"]
set_filter_map = {"Tutti": None}
WEIGHT_FILTER_OPTIONS = ["Tutti", "<= 0.5g", "0.5g - 2g", ">= 2g"]
PACK_FILTER_OPTIONS = ["Tutti", "<= 2cm", "2cm - 4cm", ">= 4cm"]
color_var = None
set_var = None
piece_type_var = None
# 🔥 CACHE per evitare rigenerazioni dropdown inutili
prev_colori_cache = []
cerca_var = None  # filtro testuale libero
label_progresso_set = None  # barra progresso set corrente
prev_set_labels_cache = []
ordine_var = None
colonne_var = None
icon_size_var = None
solo_mancanti_var = None
togli_completi_var = None
togli_zero_var = None
weight_filter_var = None
pack_filter_var = None
menu_set = None
grid_visible_items = []
grid_source_set = None
grid_row_height = 185
grid_col_width = 155
grid_row_buffer = 3
grid_last_window = (-1, -1)
grid_refresh_job = None
grid_cell_pool = []
grid_content_size = (0, 0)
set_menu_dynamic_width = 14
SET_LABEL_SUFFIX = "  "
SET_THUMB_SIZE = (140, 105)      # larghezza x altezza miniatura set
set_img_url_mem = {}             # set_code -> URL stringa (in memory)
set_photo_cache = {}             # set_code -> PhotoImage (in memory)
piece_set_names_cache = {}
piece_cache_signature = None
last_hover_result_text = None
hover_piece_key = None
pending_hover_key = None
hover_update_job = None
HOVER_UPDATE_DELAY_MS = 12
hover_clear_job = None


def _set_cell_hover_visual(cell_ref, is_hover):
    if not cell_ref:
        return
    try:
        frame = cell_ref.get("frame")
        if frame is None:
            return
        # Manteniamo thickness costante per evitare relayout costosi durante hover rapido.
        if is_hover:
            frame.config(highlightthickness=1, highlightbackground="#8ab6d6", highlightcolor="#8ab6d6")
        else:
            frame.config(highlightthickness=1, highlightbackground="#d5d5d5", highlightcolor="#d5d5d5")
    except Exception:
        pass


def _set_hover_piece_key(new_key):
    global hover_piece_key
    if hover_piece_key == new_key:
        return
    if hover_piece_key and hover_piece_key in ui_refs:
        _set_cell_hover_visual(ui_refs[hover_piece_key], is_hover=False)
    hover_piece_key = new_key
    if hover_piece_key and hover_piece_key in ui_refs:
        _set_cell_hover_visual(ui_refs[hover_piece_key], is_hover=True)

def get_all_sets_label():
    return f"Tutti{SET_LABEL_SUFFIX}"

def apply_set_menu_width():
    if menu_set is None:
        return
    try:
        width = max(12, set_menu_dynamic_width)
        menu_set.config(width=width)
    except Exception:
        pass

def aggiorna_layout_griglia():
    global grid_col_width, grid_row_height
    grid_col_width = max(120, ICON_SIZE + 15)
    grid_row_height = max(185, ICON_SIZE + 80)

def aggiorna_font_griglia():
    global normal_font, bold_font
    base_default = font.nametofont("TkDefaultFont")
    target_size = max(9, min(18, int(ICON_SIZE / 11)))

    normal_font = base_default.copy()
    normal_font.configure(size=target_size, weight="normal")

    bold_font = base_default.copy()
    bold_font.configure(size=target_size, weight="bold")

def aggiorna_dimensione_root():
    try:
        if root is None or not root.winfo_exists():
            return

        root.update_idletasks()
        target_w = max(1100, colonne * grid_col_width + 90)
        target_h = max(780, 260 + (4 * grid_row_height))
        root.geometry(f"{int(target_w)}x{int(target_h)}")
    except Exception:
        pass

def on_colonne_change(value):
    global colonne
    try:
        colonne = max(1, int(value))
    except Exception:
        return

    aggiorna_layout_griglia()
    aggiorna_dimensione_root()
    aggiorna_griglia()

def on_icon_size_change(value):
    global ICON_SIZE
    try:
        ICON_SIZE = max(40, int(value))
    except Exception:
        return

    image_cache.clear()
    aggiorna_layout_griglia()
    aggiorna_font_griglia()
    aggiorna_dimensione_root()
    aggiorna_griglia(force=True)

def on_sort_mode_change(value):
    if ordine_var is not None:
        ordine_var.set(value)
    aggiorna_griglia()
    aggiorna_batch_da_filtri(reset_to_first=True)

def on_weight_filter_change(value):
    if weight_filter_var is not None:
        weight_filter_var.set(value)
    aggiorna_griglia()
    aggiorna_batch_da_filtri()

def on_pack_filter_change(value):
    if pack_filter_var is not None:
        pack_filter_var.set(value)
    aggiorna_griglia()
    aggiorna_batch_da_filtri()

def aggiorna_colori():
    global colori_disponibili, prev_colori_cache
    selected_color = color_var.get() if color_var is not None else "Tutti"
    source_parts, _ = get_source_parts()

    colori_reali = sorted({k.split("_")[1] for k in source_parts.keys()})
    colori_normali = [c for c in colori_reali if not is_trans_color(c)]
    colori_trans_reali = [c for c in colori_reali if is_trans_color(c)]
    colori_disponibili = ["Tutti"] + colori_normali + (["Trans"] if colori_trans_reali else [])

    # I contatori cambiano anche quando la lista colori non cambia,
    # quindi il menu va rigenerato sempre.
    prev_colori_cache = colori_disponibili.copy()

    if selected_color not in colori_disponibili:
        selected_color = "Tutti"
    color_var.set(selected_color)
    
    # Calcola pezzi messi/totali per colore
    colore_stats = {}
    for k, v in source_parts.items():
        colore_reale = k.split("_")[1]
        colore = "Trans" if is_trans_color(colore_reale) else colore_reale
        if colore not in colore_stats:
            colore_stats[colore] = {"messi": 0, "totali": 0}
        messi = v.get("used", 0)
        totali = v.get("total", 0)
        colore_stats[colore]["messi"] += messi
        colore_stats[colore]["totali"] += totali
    
    # Totali per "Tutti"
    tutti_messi = sum(s["messi"] for s in colore_stats.values())
    tutti_totali = sum(s["totali"] for s in colore_stats.values())
    
    menu_colore['menu'].delete(0, 'end')
    for c in colori_disponibili:
        if c == "Tutti":
            label = f"Tutti ({tutti_messi}/{tutti_totali})"
        else:
            messi = colore_stats.get(c, {}).get("messi", 0)
            totali = colore_stats.get(c, {}).get("totali", 0)
            label = f"{c} ({messi}/{totali})"
        menu_colore['menu'].add_command(
            label=label,
            command=lambda col=c: on_color_filter_change(col)
        )

def _run_refresh_visible_grid():
    global grid_refresh_job
    grid_refresh_job = None
    refresh_visible_grid()

def schedule_refresh_visible_grid(force=False):
    global grid_refresh_job
    if force:
        if grid_refresh_job is not None:
            try:
                root.after_cancel(grid_refresh_job)
            except Exception:
                pass
            grid_refresh_job = None
        refresh_visible_grid(force=True)
        return

    if grid_refresh_job is None:
        grid_refresh_job = root.after(12, _run_refresh_visible_grid)

def refresh_visible_grid(force=False):
    global ui_refs, grid_last_window, grid_cell_pool, grid_content_size

    if 'canvas' not in globals() or 'frame_grid' not in globals():
        return
    if not canvas.winfo_exists() or not frame_grid.winfo_exists():
        return

    total_items = len(grid_visible_items)
    total_rows = (total_items + colonne - 1) // colonne if total_items else 0
    content_width = max(canvas.winfo_width(), colonne * grid_col_width)
    content_height = max(1, total_rows * grid_row_height)

    # Evita configure ridondanti che possono innescare loop di refresh UI.
    if grid_content_size != (content_width, content_height):
        frame_grid.configure(width=content_width, height=content_height)
        canvas.configure(scrollregion=(0, 0, content_width, content_height))
        grid_content_size = (content_width, content_height)

    if total_items == 0:
        for cell in grid_cell_pool:
            cell["frame"].place_forget()
        ui_refs.clear()
        grid_last_window = (-1, -1)
        return

    top = max(canvas.canvasy(0), 0)
    bottom = top + max(canvas.winfo_height(), 1)
    first_row = max(0, int(top // grid_row_height) - grid_row_buffer)
    last_row = min(total_rows - 1, int(bottom // grid_row_height) + grid_row_buffer)
    current_window = (first_row, last_row)

    if not force and current_window == grid_last_window:
        return

    grid_last_window = current_window

    start_idx = first_row * colonne
    end_idx = min(total_items, (last_row + 1) * colonne)
    show_missing_only = solo_mancanti_var.get()

    visible_count = end_idx - start_idx
    needed_rows = (visible_count + colonne - 1) // colonne if visible_count else 0
    needed_slots = max(0, needed_rows * colonne)

    while len(grid_cell_pool) < needed_slots:
        f = tk.Frame(
            frame_grid,
            highlightthickness=1,
            highlightbackground="#d5d5d5",
            highlightcolor="#d5d5d5"
        )

        b = tk.Button(f)
        b.pack()

        lbl = tk.Label(f, font=normal_font)
        lbl.pack(fill="x")
        lbl.config(justify="left", anchor="w", wraplength=max(80, grid_col_width - 10))

        cell = {
            "frame": f,
            "button": b,
            "label": lbl,
            "img_normal": None,
            "img_dark": None,
            "current_key": None,
            "source_set": None
        }

        def _on_left_click(e, c=cell):
            k = c["current_key"]
            if not k:
                return
            if "[No Color/Any Color]" in k:
                mostra_immagine_grande(k)
            else:
                click_pezzo(k)

        b.bind("<Button-1>", _on_left_click)
        bind_right_click(b, lambda e, c=cell: inserisci_quantita(c["current_key"]) if c["current_key"] else None)
        b.bind("<Enter>", lambda e, c=cell: on_hover_pezzo(e, c["current_key"], c) if c["current_key"] else None)
        b.bind("<Leave>", lambda e, c=cell: on_leave_pezzo(e, c))

        grid_cell_pool.append(cell)

    ui_refs.clear()

    for slot, i in enumerate(range(start_idx, end_idx)):
        k, v = grid_visible_items[i]
        row = i // colonne
        col = i % colonne

        cell = grid_cell_pool[slot]
        f = cell["frame"]
        b = cell["button"]
        lbl = cell["label"]

        img, img_dark = load_image_pair(k)
        cell["img_normal"] = img
        cell["img_dark"] = img_dark
        cell["current_key"] = k
        cell["source_set"] = grid_source_set

        b.config(image=img)
        b.image = img

        f.place(
            x=col * grid_col_width + 2,
            y=row * grid_row_height + 2,
            width=grid_col_width - 4,
            height=grid_row_height - 4
        )

        used, total = get_piece_counts(k, grid_source_set)
        if not lbl.winfo_manager():
            lbl.pack(fill="x")
        lbl.config(
            text=build_piece_info_text(k, v, used, total),
            wraplength=max(80, grid_col_width - 10)
        )

        completo = total > 0 and used >= total
        if show_missing_only and completo:
            b.config(image=img_dark, state="normal")
            lbl.config(fg="gray")
        else:
            b.config(image=img, state="normal")
            lbl.config(fg="black")

        ui_refs[k] = {
            "frame": f,
            "button": b,
            "label": lbl,
            "img_normal": img,
            "img_dark": img_dark,
            "source_set": grid_source_set
        }

        _set_cell_hover_visual(ui_refs[k], is_hover=(k == hover_piece_key))

        if k == last_selected:
            lbl.config(font=bold_font, bg="#d9edf7")
        else:
            lbl.config(font=normal_font, bg=lbl.master.cget("bg"))

    for slot in range(visible_count, len(grid_cell_pool)):
        grid_cell_pool[slot]["frame"].place_forget()

def render_griglia():
    global grid_visible_items, grid_source_set, grid_last_window, prev_filter_state

    selected_color = color_var.get()
    source_parts, source_set = get_source_parts()
    current_sort = ordine_var.get() if ordine_var is not None else "Stud"
    
    # 🔥 CACHE: se filtri non sono cambiati, salta render costoso
    solo_mancanti = bool(solo_mancanti_var.get()) if solo_mancanti_var is not None else False
    togli_completi = bool(togli_completi_var.get()) if togli_completi_var is not None else False
    togli_zero = bool(togli_zero_var.get()) if togli_zero_var is not None else False
    cerca_text = cerca_var.get().strip().lower() if cerca_var is not None else ""
    selected_piece_type = piece_type_var.get() if piece_type_var is not None else "Tutti"
    current_state = {"color": selected_color, "set": source_set, "sort": current_sort, "solo_mancanti": solo_mancanti, "togli_completi": togli_completi, "togli_zero": togli_zero, "cerca": cerca_text, "piece_type": selected_piece_type}
    if current_state == prev_filter_state and grid_visible_items:
        # Filtri uguali, solo refresh viewport
        schedule_refresh_visible_grid(force=False)
        return
    
    prev_filter_state = current_state.copy()

    pezzi = []
    for k, v in source_parts.items():
        colore = k.split("_")[1]
        if selected_color != "Tutti":
            if selected_color == "Trans":
                if not is_trans_color(colore):
                    continue
            elif colore != selected_color:
                continue
        part_num = k.rsplit("_", 1)[0]
        if not passa_filtri_dimensionali(part_num):
            continue
        if selected_piece_type != "Tutti" and get_piece_type(v.get("name", ""), part_num) != selected_piece_type:
            continue
        if cerca_text and cerca_text not in v.get("name", "").lower() and cerca_text not in part_num.lower():
            continue
        if togli_zero:
            used, _total = get_piece_counts(k, source_set)
            if used <= 0:
                continue
        if togli_completi:
            used, total = get_piece_counts(k, source_set)
            if total > 0 and used >= total:
                continue
        pezzi.append((k, v))
    if current_sort == "Stud":
        pezzi.sort(
            key=lambda item: (
                lunghezza_pezzo_valore(item[1].get("name", ""), item[0].rsplit("_", 1)[0]),
                get_weight_g(item[0].rsplit("_", 1)[0]) or -1.0,
                item[1].get("total", 0)
            ),
            reverse=True
        )
    elif current_sort == "Peso":
        pezzi.sort(
            key=lambda item: (
                get_weight_g(item[0].rsplit("_", 1)[0]) or -1.0,
                lunghezza_pezzo_valore(item[1].get("name", ""), item[0].rsplit("_", 1)[0]),
                item[1].get("total", 0)
            ),
            reverse=True
        )
    elif current_sort == "Dimensioni":
        pezzi.sort(
            key=lambda item: (
                get_pack_max_cm(item[0].rsplit("_", 1)[0]) or -1.0,
                get_weight_g(item[0].rsplit("_", 1)[0]) or -1.0,
                lunghezza_pezzo_valore(item[1].get("name", ""), item[0].rsplit("_", 1)[0])
            ),
            reverse=True
        )
    else:
        # Quantita
        pezzi.sort(
            key=lambda item: (
                item[1].get("total", 0),
                lunghezza_pezzo_valore(item[1].get("name", ""), item[0].rsplit("_", 1)[0])
            ),
            reverse=True
        )
    
    grid_visible_items = pezzi
    grid_source_set = source_set
    grid_last_window = (-1, -1)
    schedule_refresh_visible_grid(force=True)

def crea_griglia():
    render_griglia()

def aggiorna_griglia(force=False):
    global prev_filter_state
    if force:
        prev_filter_state = {}  # invalida cache per forzare re-render
    render_griglia()

def aggiorna_selezione_ui(prev_selected, current_selected):
    """Aggiorna i widget di selezione per mostrare solo l'ultimo pezzo cliccato."""
    if prev_selected and prev_selected in ui_refs:
        lbl = ui_refs[prev_selected]["label"]
        lbl.config(font=normal_font, bg=lbl.master.cget("bg"))
        try:
            lbl.update_idletasks()
        except Exception:
            pass

    if current_selected and current_selected in ui_refs:
        lbl = ui_refs[current_selected]["label"]
        lbl.config(font=bold_font, bg="#d9edf7")
        try:
            lbl.update_idletasks()
        except Exception:
            pass


def aggiorna_pezzo_griglia(k):
    """Aggiorna SOLO il pezzo k senza ricreare tutta la griglia 🚀"""
    if k not in ui_refs or k not in global_parts:
        return

    source_set = ui_refs[k].get("source_set")
    if source_set and source_set not in sets:
        return

    v = sets[source_set][k] if source_set else global_parts[k]
    used, total = get_piece_counts(k, source_set)
    
    # Aggiorna solo il label del pezzo
    lbl = ui_refs[k]["label"]
    lbl.config(
        text=build_piece_info_text(k, v, used, total),
        wraplength=max(80, grid_col_width - 10)
    )
    
    # Se il pezzo è completato, color verde
    colore = "green" if total > 0 and used >= total else "black"
    lbl.config(fg=colore)

    # Aggiorna subito icona/stato bottone in base al toggle "solo mancanti"
    btn = ui_refs[k]["button"]
    img_normal = ui_refs[k]["img_normal"]
    img_dark = ui_refs[k]["img_dark"]
    completo = total > 0 and used >= total
    if solo_mancanti_var.get() and completo:
        btn.config(image=img_dark, state="normal")
        lbl.config(fg="gray")
    else:
        btn.config(image=img_normal, state="normal")
    
    # Highlight se è l'ultimo selezionato
    if k == last_selected:
        lbl.config(font=bold_font, bg="#d9edf7")
    else:
        lbl.config(font=normal_font, bg=lbl.master.cget("bg"))

    # Forza refresh della visualizzazione
    try:
        lbl.update_idletasks()
    except Exception:
        pass

def on_filter_checkbox_change():
    """Aggiorna griglia e salva subito i checkbox filtro nella UI settings."""
    aggiorna_griglia(force=True)
    save_ui_settings()

def on_lock_checkbox_change():
    """Salva subito lo stato del lock nella UI settings."""
    save_ui_settings()

# ------------------------
# MAGIC: SET COMPLETABILI
# ------------------------
def apri_magic_sets():
    global magic_win

    if magic_win is not None and magic_win.winfo_exists():
        magic_win.lift()
        magic_win.focus_force()
        return

    if not sets:
        messagebox.showinfo("Magic Sets", "Nessun set caricato!")
        return

    # --- calcola pezzi liberi (non ancora usati) aggregati su tutti i set ---
    available = {}
    for nome, s in sets.items():
        if nome in disabled_sets:
            continue
        for k, v in s.items():
            free = v.get("total", 0) - v.get("used", 0)
            if free > 0:
                available[k] = available.get(k, 0) + free

    # --- per ogni set calcola copertura con i pezzi liberi ---
    risultati = []
    for nome, s in sets.items():
        if nome in disabled_sets:
            continue
        needed = {}
        for k, v in s.items():
            still_need = v.get("total", 0) - v.get("used", 0)
            if still_need > 0:
                needed[k] = still_need
        if not needed:
            continue  # già completo, salta
        total_needed = sum(needed.values())
        total_coverable = sum(min(qty, available.get(k, 0)) for k, qty in needed.items())
        perc = int(total_coverable / total_needed * 100) if total_needed > 0 else 100
        risultati.append((nome, perc, needed, total_needed, total_coverable))

    risultati.sort(key=lambda x: x[1], reverse=True)

    # ---- finestra principale ----
    magic_win = tk.Toplevel(root)
    magic_win.title("🪄 Set completabili con pezzi disponibili")
    magic_win.withdraw()

    # intestazione
    frame_header = tk.Frame(magic_win)
    frame_header.pack(fill="x", padx=10, pady=(8, 2))
    tk.Label(
        frame_header,
        text=f"Set ordinati per % completabile con i pezzi non ancora smistati ({len(risultati)} set con pezzi mancanti)",
        font=("Arial", 12, "bold"),
        anchor="w"
    ).pack(side="left")
    tk.Button(frame_header, text="Chiudi", command=magic_win.destroy).pack(side="right")

    # canvas scrollabile
    canvas_m = tk.Canvas(magic_win)
    bind_mousewheel_scroll(canvas_m)
    canvas_m.pack(side="left", fill="both", expand=True)
    sb_m = tk.Scrollbar(magic_win, command=canvas_m.yview)
    sb_m.pack(side="right", fill="y")
    canvas_m.configure(yscrollcommand=sb_m.set)

    frame_m = tk.Frame(canvas_m)
    canvas_m.create_window((0, 0), window=frame_m, anchor="nw")
    frame_m.bind(
        "<Configure>",
        lambda e: canvas_m.configure(scrollregion=canvas_m.bbox("all"))
    )

    # ---- finestra dettaglio pezzi (riutilizzata) ----
    detail_state = {"win": None}

    def apri_dettaglio_magic(nome_set, needed, available_pool):
        """Mostra i pezzi mancanti per il set e quante unità libere ci sono."""
        if detail_state["win"] is not None and detail_state["win"].winfo_exists():
            detail_state["win"].destroy()

        dwin = tk.Toplevel(magic_win)
        detail_state["win"] = dwin
        dwin.withdraw()
        codice = set_codes.get(nome_set, "?")
        numero = numerazione_set.get(nome_set, "?")
        dwin.title(f"Pezzi mancanti: {numero}. {nome_set} ({codice})")

        frame_dh = tk.Frame(dwin)
        frame_dh.pack(fill="x", padx=8, pady=(8, 2))

        # miniatura set
        thumb_lbl_d = tk.Label(frame_dh, width=SET_THUMB_SIZE[0], height=SET_THUMB_SIZE[1],
                               bg="#dddddd", relief="flat")
        thumb_lbl_d.pack(side="left", padx=(0, 10))
        load_set_thumbnail(codice, thumb_lbl_d)

        info_frame = tk.Frame(frame_dh)
        info_frame.pack(side="left", fill="x", expand=True)
        tk.Label(info_frame, text=f"{numero}. {nome_set}", font=("Arial", 13, "bold"), anchor="w").pack(fill="x")
        tk.Label(info_frame, text=f"Codice: {codice}", anchor="w", fg="#555555").pack(fill="x")

        tk.Button(frame_dh, text="Chiudi", command=dwin.destroy).pack(side="right")

        # lista pezzi scrollabile
        canvas_d = tk.Canvas(dwin)
        bind_mousewheel_scroll(canvas_d)
        canvas_d.pack(side="left", fill="both", expand=True)
        sb_d = tk.Scrollbar(dwin, command=canvas_d.yview)
        sb_d.pack(side="right", fill="y")
        canvas_d.configure(yscrollcommand=sb_d.set)
        frame_d = tk.Frame(canvas_d)
        canvas_d.create_window((0, 0), window=frame_d, anchor="nw")
        frame_d.bind("<Configure>", lambda e: canvas_d.configure(scrollregion=canvas_d.bbox("all")))

        # intestazione colonne
        tk.Label(frame_d, text="Immagine", font=("Arial", 10, "bold"), width=12).grid(row=0, column=0, padx=6, pady=2)
        tk.Label(frame_d, text="Pezzo", font=("Arial", 10, "bold"), anchor="w").grid(row=0, column=1, sticky="w", padx=6)
        tk.Label(frame_d, text="Servono", font=("Arial", 10, "bold"), width=8).grid(row=0, column=2, padx=6)
        tk.Label(frame_d, text="Disponibili", font=("Arial", 10, "bold"), width=10).grid(row=0, column=3, padx=6)
        tk.Label(frame_d, text="Stato", font=("Arial", 10, "bold"), width=10).grid(row=0, column=4, padx=6)

        set_data = sets.get(nome_set, {})
        sorted_needed = sorted(needed.items(), key=lambda x: available_pool.get(x[0], 0) - x[1])

        for riga, (k, qty_need) in enumerate(sorted_needed, start=1):
            avail = available_pool.get(k, 0)
            ok = avail >= qty_need
            colore_stato = "#2e7d32" if ok else "#b71c1c"
            stato_txt = "OK ✓" if ok else f"Mancano {qty_need - avail}"

            # immagine pezzo (piccola)
            img_tk_s, _ = load_image_pair(k, size=60)
            if img_tk_s:
                lbl_i = tk.Label(frame_d, image=img_tk_s)
                lbl_i.image = img_tk_s
            else:
                lbl_i = tk.Label(frame_d, text="…", width=8, fg="#aaaaaa")
                url_img = set_data.get(k, {}).get("img", "")
                load_image_pair_async(k, 60, lbl_i, url_fallback=url_img)
            lbl_i.grid(row=riga, column=0, padx=4, pady=2)

            nome_pezzo = set_data.get(k, {}).get("name", k)
            tk.Label(frame_d, text=nome_pezzo, anchor="w", wraplength=320, justify="left").grid(
                row=riga, column=1, sticky="w", padx=6, pady=2
            )
            tk.Label(frame_d, text=str(qty_need), width=8, anchor="center").grid(row=riga, column=2, padx=6)
            tk.Label(frame_d, text=str(avail), width=10, anchor="center").grid(row=riga, column=3, padx=6)
            tk.Label(frame_d, text=stato_txt, width=10, fg=colore_stato, anchor="center").grid(row=riga, column=4, padx=6)

        dwin.update_idletasks()
        w = min(max(700, frame_d.winfo_reqwidth() + sb_d.winfo_reqwidth() + 30), 1400)
        h = min(max(350, frame_d.winfo_reqheight() + 120), 900)
        # posizione relativa alla finestra magic
        try:
            mx = magic_win.winfo_rootx() + 40
            my = magic_win.winfo_rooty() + 40
        except Exception:
            mx, my = 100, 100
        dwin.geometry(f"{w}x{h}+{mx}+{my}")
        dwin.deiconify()
        dwin.lift()

    # ---- righe set ----
    THUMB_W, THUMB_H = SET_THUMB_SIZE

    for idx, (nome, perc, needed, total_needed, total_coverable) in enumerate(risultati):
        codice = set_codes.get(nome, "?")
        numero = numerazione_set.get(nome, "?")
        row_bg = "#e8f5e9" if perc == 100 else ("SystemButtonFace" if idx % 2 == 0 else "#f5f5f5")

        row_frame = tk.Frame(frame_m, bg=row_bg, cursor="hand2")
        row_frame.pack(fill="x", padx=4, pady=2)

        # thumbnail
        thumb_lbl = tk.Label(row_frame, width=THUMB_W, height=THUMB_H, bg="#dddddd", relief="flat", cursor="hand2")
        thumb_lbl.pack(side="left", padx=(4, 8), pady=4)
        load_set_thumbnail(codice, thumb_lbl)
        thumb_lbl.bind("<Button-1>", lambda e, n=nome, nd=needed, av=dict(available): apri_dettaglio_magic(n, nd, av))

        # info testo
        info = tk.Frame(row_frame, bg=row_bg)
        info.pack(side="left", fill="x", expand=True, pady=4)

        tk.Label(
            info,
            text=f"{numero}. {nome}",
            font=("Arial", 12, "bold"),
            anchor="w",
            bg=row_bg,
            cursor="hand2"
        ).pack(fill="x")
        tk.Label(
            info,
            text=f"Codice: {codice}   Pezzi mancanti: {total_needed}   Coperti da disponibili: {total_coverable}",
            anchor="w",
            fg="#444444",
            bg=row_bg
        ).pack(fill="x")

        # barra progresso
        bar_frame = tk.Frame(info, bg=row_bg)
        bar_frame.pack(fill="x", pady=(2, 0))
        bar_w = 340
        bar_h = 10
        bar_c = tk.Canvas(bar_frame, width=bar_w, height=bar_h, highlightthickness=0, bg=row_bg)
        bar_c.pack(side="left")
        bar_c.create_rectangle(0, 0, bar_w, bar_h, fill="#dddddd", outline="")
        fill_w = int(bar_w * perc / 100)
        fill_color = "#21a366" if perc == 100 else "#1976d2"
        if fill_w > 0:
            bar_c.create_rectangle(0, 0, fill_w, bar_h, fill=fill_color, outline="")
        tk.Label(bar_frame, text=f"{perc}%", font=("Arial", 10, "bold"),
                 fg=fill_color, bg=row_bg).pack(side="left", padx=6)

        # click riga → dettaglio
        def _on_click(e, n=nome, nd=needed, av=dict(available)):
            apri_dettaglio_magic(n, nd, av)

        for w in (row_frame, info):
            w.bind("<Button-1>", _on_click)
        for child in info.winfo_children():
            child.bind("<Button-1>", _on_click)

    # dimensiona finestra
    magic_win.update_idletasks()
    w = min(max(700, frame_m.winfo_reqwidth() + sb_m.winfo_reqwidth() + 30), 1300)
    h = min(max(400, frame_m.winfo_reqheight() + 80), 950)
    try:
        rx = root.winfo_rootx() + 80
        ry = root.winfo_rooty() + 80
    except Exception:
        rx, ry = 80, 80
    magic_win.geometry(f"{w}x{h}+{rx}+{ry}")

    def _on_magic_close():
        global magic_win
        if detail_state["win"] is not None and detail_state["win"].winfo_exists():
            detail_state["win"].destroy()
        magic_win.destroy()
        magic_win = None

    magic_win.protocol("WM_DELETE_WINDOW", _on_magic_close)
    magic_win.deiconify()
    magic_win.lift()
    magic_win.focus_force()

# ------------------------
# CERCA NUOVI SET SU REBRICKABLE
# ------------------------
cerca_nuovi_win = None

def apri_cerca_nuovi_sets():
    global cerca_nuovi_win

    if cerca_nuovi_win is not None and cerca_nuovi_win.winfo_exists():
        cerca_nuovi_win.lift()
        cerca_nuovi_win.focus_force()
        return

    # --- pezzi liberi dell'utente ---
    available = {}
    for nome, s in sets.items():
        if nome in disabled_sets:
            continue
        for k, v in s.items():
            free = v.get("total", 0) - v.get("used", 0)
            if free > 0:
                available[k] = available.get(k, 0) + free

    if not available:
        messagebox.showinfo("Cerca Nuovi Set", "Nessun pezzo libero disponibile!")
        return

    owned_codes = set(set_codes.values())

    cerca_nuovi_win = tk.Toplevel(root)
    cerca_nuovi_win.title("🔍 Cerca set nuovi su Rebrickable")
    cerca_nuovi_win.geometry("980x760")

    # ---- parametri ricerca ----
    frame_params = tk.Frame(cerca_nuovi_win, pady=8)
    frame_params.pack(fill="x", padx=10)

    tk.Label(frame_params, text="Min pezzi:").grid(row=0, column=0, sticky="w", padx=4)
    entry_min = tk.Entry(frame_params, width=7)
    entry_min.insert(0, "10")
    entry_min.grid(row=0, column=1, padx=4)

    tk.Label(frame_params, text="Max pezzi:").grid(row=0, column=2, sticky="w", padx=4)
    entry_max = tk.Entry(frame_params, width=7)
    entry_max.insert(0, "500")
    entry_max.grid(row=0, column=3, padx=4)

    tk.Label(frame_params, text="Anno min:").grid(row=0, column=4, sticky="w", padx=4)
    entry_year_min = tk.Entry(frame_params, width=6)
    entry_year_min.insert(0, "2015")
    entry_year_min.grid(row=0, column=5, padx=4)

    tk.Label(frame_params, text="Anno max:").grid(row=0, column=6, sticky="w", padx=4)
    entry_year_max = tk.Entry(frame_params, width=6)
    entry_year_max.insert(0, "2026")
    entry_year_max.grid(row=0, column=7, padx=4)

    tk.Label(frame_params, text="Max set da analizzare:").grid(row=1, column=0, sticky="w", padx=4, pady=4)
    entry_max_sets = tk.Entry(frame_params, width=6)
    entry_max_sets.insert(0, "80")
    entry_max_sets.grid(row=1, column=1, padx=4)

    tk.Label(frame_params, text="% min copertura:").grid(row=1, column=2, sticky="w", padx=4)
    entry_min_perc = tk.Entry(frame_params, width=6)
    entry_min_perc.insert(0, "50")
    entry_min_perc.grid(row=1, column=3, padx=4)

    btn_cerca_r = tk.Button(frame_params, text="🔍 Cerca", font=("Arial", 11, "bold"))
    btn_cerca_r.grid(row=0, column=8, rowspan=2, padx=10, pady=4)
    btn_stop_r = tk.Button(frame_params, text="⏹ Stop", font=("Arial", 11, "bold"), state="disabled")
    btn_stop_r.grid(row=0, column=9, rowspan=2, padx=4, pady=4)

    # ---- barra status ----
    frame_status = tk.Frame(cerca_nuovi_win)
    frame_status.pack(fill="x", padx=10, pady=2)
    lbl_status = tk.Label(frame_status, text="Pronto. Imposta i parametri e clicca Cerca.", anchor="w")
    lbl_status.pack(side="left", fill="x", expand=True)
    lbl_avail = tk.Label(frame_status,
                         text=f"Pezzi liberi disponibili: {len(available)} tipi",
                         anchor="e", fg="#1976d2")
    lbl_avail.pack(side="right")

    # ---- pannello principale (risultati + console) ----
    paned = tk.PanedWindow(cerca_nuovi_win, orient="vertical", sashrelief="raised", sashwidth=5)
    paned.pack(fill="both", expand=True, padx=0, pady=0)

    # ---- canvas risultati ----
    frame_risultati = tk.Frame(paned)
    paned.add(frame_risultati, stretch="always", minsize=200)
    canvas_r = tk.Canvas(frame_risultati)
    bind_mousewheel_scroll(canvas_r)
    canvas_r.pack(side="left", fill="both", expand=True)
    sb_r = tk.Scrollbar(frame_risultati, command=canvas_r.yview)
    sb_r.pack(side="right", fill="y")
    canvas_r.configure(yscrollcommand=sb_r.set)
    frame_r = tk.Frame(canvas_r)
    canvas_r.create_window((0, 0), window=frame_r, anchor="nw")
    frame_r.bind("<Configure>", lambda e: canvas_r.configure(scrollregion=canvas_r.bbox("all")))

    # ---- console log ----
    frame_console = tk.Frame(paned)
    paned.add(frame_console, stretch="never", minsize=80)
    frame_console_hdr = tk.Frame(frame_console)
    frame_console_hdr.pack(fill="x")
    tk.Label(frame_console_hdr, text="Console", font=("Arial", 9, "bold"), anchor="w").pack(side="left", padx=4)
    btn_clear_log = tk.Button(frame_console_hdr, text="Pulisci", font=("Arial", 8),
                              command=lambda: (txt_console.config(state="normal"),
                                              txt_console.delete("1.0", tk.END),
                                              txt_console.config(state="disabled")))
    btn_clear_log.pack(side="right", padx=4)
    txt_console = tk.Text(frame_console, height=7, font=("Courier", 10),
                          bg="#1e1e1e", fg="#d4d4d4", insertbackground="white",
                          wrap="word", bd=0, relief="flat", state="disabled")
    txt_console.pack(fill="both", expand=True, padx=2, pady=(0, 2))
    txt_console.tag_config("ok", foreground="#6fcf97")
    txt_console.tag_config("warn", foreground="#f2c94c")
    txt_console.tag_config("err", foreground="#eb5757")
    txt_console.tag_config("info", foreground="#56b6c2")

    def log_console(msg, tag=""):
        """Appende una riga alla console log (thread-safe via root.after)."""
        def _append():
            try:
                if not txt_console.winfo_exists():
                    return
                txt_console.config(state="normal")
                txt_console.insert(tk.END, msg + "\n", tag)
                txt_console.see(tk.END)
                txt_console.config(state="disabled")
            except Exception:
                pass
        root.after(0, _append)

    search_state = {"running": False, "stop": False}
    detail_state = {"win": None}

    # ---- finestra dettaglio pezzi del set trovato ----
    def apri_dettaglio_nuovo(codice, nome, parts_data, available_pool):
        if detail_state["win"] is not None and detail_state["win"].winfo_exists():
            detail_state["win"].destroy()

        dwin = tk.Toplevel(cerca_nuovi_win)
        detail_state["win"] = dwin
        dwin.withdraw()
        dwin.title(f"Pezzi: {codice} — {nome}")

        frame_dh = tk.Frame(dwin)
        frame_dh.pack(fill="x", padx=8, pady=(8, 2))

        thumb_lbl_d = tk.Label(frame_dh, width=SET_THUMB_SIZE[0], height=SET_THUMB_SIZE[1],
                               bg="#dddddd", relief="flat")
        thumb_lbl_d.pack(side="left", padx=(0, 10))
        load_set_thumbnail(codice, thumb_lbl_d)

        info_f = tk.Frame(frame_dh)
        info_f.pack(side="left", fill="x", expand=True)
        tk.Label(info_f, text=nome, font=("Arial", 13, "bold"), anchor="w").pack(fill="x")
        tk.Label(info_f, text=f"Codice: {codice}", anchor="w", fg="#555555").pack(fill="x")
        tk.Button(frame_dh, text="Chiudi", command=dwin.destroy).pack(side="right")

        canvas_d = tk.Canvas(dwin)
        bind_mousewheel_scroll(canvas_d)
        canvas_d.pack(side="left", fill="both", expand=True)
        sb_d = tk.Scrollbar(dwin, command=canvas_d.yview)
        sb_d.pack(side="right", fill="y")
        canvas_d.configure(yscrollcommand=sb_d.set)
        frame_d = tk.Frame(canvas_d)
        canvas_d.create_window((0, 0), window=frame_d, anchor="nw")
        frame_d.bind("<Configure>", lambda e: canvas_d.configure(scrollregion=canvas_d.bbox("all")))

        for ci, hdr in enumerate(["Imm.", "Pezzo", "Colore", "Servono", "Disponibili", "Stato"]):
            tk.Label(frame_d, text=hdr, font=("Arial", 10, "bold")).grid(row=0, column=ci, padx=6, pady=2)

        sorted_parts = sorted(
            parts_data.items(),
            key=lambda x: available_pool.get(x[0], 0) - x[1].get("total", 0)
        )

        for riga, (k, v) in enumerate(sorted_parts, start=1):
            need = v.get("total", 0)
            avail = available_pool.get(k, 0)
            ok = avail >= need
            colore_stato = "#2e7d32" if ok else "#b71c1c"
            stato_txt = "OK ✓" if ok else f"Mancano {need - avail}"

            img_tk_s, _ = load_image_pair(k, size=60)
            lbl_i = tk.Label(frame_d, width=60, height=60, bg="#eeeeee", relief="flat")
            if img_tk_s:
                lbl_i.config(image=img_tk_s)
                lbl_i.image = img_tk_s
            else:
                url_img = v.get("img", "")
                load_image_pair_async(k, 60, lbl_i, url_fallback=url_img)
            lbl_i.grid(row=riga, column=0, padx=4, pady=2)

            part_num = k.split("_")[0]
            colore_nome = k.split("_")[1] if "_" in k else ""
            tk.Label(frame_d, text=v.get("name", part_num), anchor="w", wraplength=300, justify="left").grid(
                row=riga, column=1, sticky="w", padx=6
            )
            tk.Label(frame_d, text=colore_nome, anchor="w", wraplength=120).grid(row=riga, column=2, sticky="w", padx=4)
            tk.Label(frame_d, text=str(need), width=7, anchor="center").grid(row=riga, column=3, padx=6)
            tk.Label(frame_d, text=str(avail), width=10, anchor="center").grid(row=riga, column=4, padx=6)
            tk.Label(frame_d, text=stato_txt, width=10, fg=colore_stato, anchor="center").grid(row=riga, column=5, padx=6)

        dwin.update_idletasks()
        w = min(max(750, frame_d.winfo_reqwidth() + sb_d.winfo_reqwidth() + 30), 1400)
        h = min(max(350, frame_d.winfo_reqheight() + 120), 900)
        try:
            mx = cerca_nuovi_win.winfo_rootx() + 40
            my = cerca_nuovi_win.winfo_rooty() + 40
        except Exception:
            mx, my = 120, 120
        dwin.geometry(f"{w}x{h}+{mx}+{my}")
        dwin.deiconify()
        dwin.lift()

    # ---- aggiungi riga risultato ----
    def aggiungi_riga(codice, nome, perc, parts_data, total_need, covered, available_pool, idx):
        row_bg = "#e8f5e9" if perc == 100 else ("SystemButtonFace" if idx % 2 == 0 else "#f5f5f5")

        row_frame = tk.Frame(frame_r, bg=row_bg, cursor="hand2")
        row_frame.pack(fill="x", padx=4, pady=2)

        thumb_lbl = tk.Label(row_frame, width=SET_THUMB_SIZE[0], height=SET_THUMB_SIZE[1],
                             bg="#dddddd", relief="flat", cursor="hand2")
        thumb_lbl.pack(side="left", padx=(4, 8), pady=4)
        load_set_thumbnail(codice, thumb_lbl)

        info = tk.Frame(row_frame, bg=row_bg)
        info.pack(side="left", fill="x", expand=True, pady=4)

        tk.Label(info, text=nome, font=("Arial", 12, "bold"), anchor="w",
                 bg=row_bg, cursor="hand2").pack(fill="x")
        tk.Label(info,
                 text=f"Codice: {codice}   Pezzi totali: {total_need}   Coperti con i tuoi pezzi liberi: {covered}",
                 anchor="w", fg="#444444", bg=row_bg).pack(fill="x")

        bar_frame = tk.Frame(info, bg=row_bg)
        bar_frame.pack(fill="x", pady=(2, 0))
        bar_w_px = 340
        bar_h_px = 10
        bar_c = tk.Canvas(bar_frame, width=bar_w_px, height=bar_h_px, highlightthickness=0, bg=row_bg)
        bar_c.pack(side="left")
        bar_c.create_rectangle(0, 0, bar_w_px, bar_h_px, fill="#dddddd", outline="")
        fill_w_px = int(bar_w_px * perc / 100)
        fill_color = "#21a366" if perc == 100 else "#1976d2"
        if fill_w_px > 0:
            bar_c.create_rectangle(0, 0, fill_w_px, bar_h_px, fill=fill_color, outline="")
        tk.Label(bar_frame, text=f"{perc}%", font=("Arial", 10, "bold"),
                 fg=fill_color, bg=row_bg).pack(side="left", padx=6)

        def _on_click(e, c=codice, n=nome, pd=parts_data, av=dict(available_pool)):
            apri_dettaglio_nuovo(c, n, pd, av)

        for widget in (row_frame, info):
            widget.bind("<Button-1>", _on_click)
        for child in info.winfo_children():
            child.bind("<Button-1>", _on_click)
        thumb_lbl.bind("<Button-1>", _on_click)

    # ---- logica ricerca ----
    def esegui_ricerca():
        def _reset_ui():
            search_state["running"] = False
            root.after(0, lambda: btn_cerca_r.config(state="normal"))
            root.after(0, lambda: btn_stop_r.config(state="disabled"))

        try:
            _esegui_ricerca_inner(_reset_ui)
        except Exception as _outer_exc:
            import traceback
            msg = f"❌ Errore inatteso: {_outer_exc}"
            log_console(msg, "err")
            log_console(traceback.format_exc(), "err")
            print(f"[CERCA_NUOVI][CRASH] {traceback.format_exc()}")
            root.after(0, lambda: lbl_status.config(text=msg))
            _reset_ui()

    def _esegui_ricerca_inner(_reset_ui):
        try:
            min_parts_v = int(entry_min.get() or 10)
            max_parts_v = int(entry_max.get() or 500)
            year_min_v = int(entry_year_min.get() or 2015)
            year_max_v = int(entry_year_max.get() or 2026)
            max_sets_v = max(10, min(500, int(entry_max_sets.get() or 80)))
            min_perc_v = max(0, min(100, int(entry_min_perc.get() or 50)))
        except ValueError:
            root.after(0, lambda: lbl_status.config(text="❌ Parametri non validi!"))
            log_console("❌ Parametri non validi! Controlla i campi.", "err")
            _reset_ui()
            return

        print(f"[CERCA_NUOVI] Avvio ricerca — pezzi {min_parts_v}-{max_parts_v}, anni {year_min_v}-{year_max_v}")
        root.after(0, lambda: [w.destroy() for w in frame_r.winfo_children()])
        root.after(0, lambda: lbl_status.config(text="Scarico lista set da Rebrickable..."))
        log_console(f"▶ Ricerca avviata: pezzi {min_parts_v}-{max_parts_v}, anni {year_min_v}-{year_max_v}, max {max_sets_v} set, copertura min {min_perc_v}%", "info")

        # --- recupera set da Rebrickable ---
        fetched_sets = []
        page = 1
        page_size = min(100, max_sets_v)

        while len(fetched_sets) < max_sets_v and not search_state["stop"]:
            try:
                r = requests.get(
                    "https://rebrickable.com/api/v3/lego/sets/",
                    headers={"Authorization": f"key {API_KEY}"},
                    params={
                        "min_parts": min_parts_v,
                        "max_parts": max_parts_v,
                        "min_year": year_min_v,
                        "max_year": year_max_v,
                        "page": page,
                        "page_size": page_size,
                        "ordering": "-year"
                    },
                    timeout=12
                )
                if r.status_code != 200:
                    log_console(f"⚠ API HTTP {r.status_code} — interrompo.", "warn")
                    break
                data = r.json()
                results = data.get("results", [])
                if not results:
                    break
                for s in results:
                    code = s.get("set_num", "")
                    if code and code not in owned_codes:
                        fetched_sets.append({
                            "code": code,
                            "name": s.get("name", code),
                        })
                        img_url = s.get("set_img_url", "")
                        if img_url:
                            set_img_url_mem[code] = img_url
                log_console(f"  Pagina {page}: {len(results)} set ricevuti (totale finora: {len(fetched_sets)})", "")
                if not data.get("next"):
                    break
                page += 1
            except Exception as e:
                log_console(f"❌ Errore rete pagina {page}: {e}", "err")
                print(f"[CERCA_NUOVI] Errore lista set: {e}")
                break

        if search_state["stop"]:
            root.after(0, lambda: lbl_status.config(text="⏹ Ricerca interrotta."))
            log_console("⏹ Ricerca interrotta dall'utente.", "warn")
            _reset_ui()
            return

        total_found = len(fetched_sets)
        root.after(0, lambda: lbl_status.config(
            text=f"Trovati {total_found} set nuovi. Analizzo parti..."))
        log_console(f"✔ Trovati {total_found} set nuovi (esclusi i tuoi). Analizzo le parti...", "ok")

        analizzati = []

        # --- carica offline cache una sola volta ---
        offline_data = load_offline_sets()
        offline_codice_to_nome = {c: n for n, c in offline_data["set_codes"].items()}

        def analizza_set(info_set):
            if search_state["stop"]:
                return None
            codice = info_set["code"]
            nome_set = info_set["name"]

            # Prima cerca nella cache offline
            offline_nome = offline_codice_to_nome.get(codice)
            if offline_nome and offline_nome in offline_data["parts"]:
                parts = offline_data["parts"][offline_nome]
                log_console(f"  [cache] {codice} — {nome_set}", "")
            else:
                try:
                    log_console(f"  [download] {codice} — {nome_set}", "info")
                    parts = scarica_set(codice)
                    if not parts:
                        log_console(f"  ⚠ {codice}: nessun pezzo restituito", "warn")
                        return None
                except Exception as e:
                    log_console(f"  ❌ {codice}: errore download — {e}", "err")
                    return None

            total_need = sum(v.get("total", 0) for v in parts.values())
            if total_need == 0:
                return None
            covered = sum(min(v.get("total", 0), available.get(k, 0)) for k, v in parts.items())
            perc = int(covered / total_need * 100)

            if perc < min_perc_v:
                return None
            return (codice, nome_set, perc, parts, total_need, covered)

        with ThreadPoolExecutor(max_workers=MAX_THREADS) as ex:
            futures = []
            for info_set in fetched_sets[:max_sets_v]:
                if search_state["stop"]:
                    break
                futures.append(ex.submit(analizza_set, info_set))

            done_count = 0
            for fut in futures:
                if search_state["stop"]:
                    break
                try:
                    res = fut.result(timeout=30)
                    done_count += 1
                    if res:
                        analizzati.append(res)
                        log_console(f"  ✅ {res[0]} — {res[1]}  {res[2]}% copertura", "ok")
                    cnt_ok = len(analizzati)
                    root.after(0, lambda d=done_count, t=total_found, ok=cnt_ok:
                               lbl_status.config(
                                   text=f"Analizzati {d}/{t}... trovati {ok} compatibili"))
                except Exception as ex_e:
                    done_count += 1
                    log_console(f"  ❌ Errore analisi set: {ex_e}", "err")

        if search_state["stop"]:
            root.after(0, lambda: lbl_status.config(text="⏹ Ricerca interrotta."))
            log_console("⏹ Ricerca interrotta dall'utente durante analisi.", "warn")
            _reset_ui()
            return

        analizzati.sort(key=lambda x: x[2], reverse=True)
        log_console(f"─── Completato: {len(analizzati)} set compatibili su {total_found} analizzati ───", "ok")

        def mostra_risultati():
            for w in frame_r.winfo_children():
                w.destroy()
            if not analizzati:
                tk.Label(frame_r,
                         text="Nessun set trovato con i tuoi pezzi liberi.",
                         font=("Arial", 12)).pack(pady=20)
            else:
                for idx, (codice, nome_s, perc, parts, total_need, covered) in enumerate(analizzati):
                    aggiungi_riga(codice, nome_s, perc, parts, total_need, covered, available, idx)
            lbl_status.config(
                text=f"✅ Completato: {len(analizzati)} set compatibili su {total_found} analizzati.")

        root.after(0, mostra_risultati)
        _reset_ui()

    def avvia_ricerca():
        if search_state["running"]:
            return
        search_state["running"] = True
        search_state["stop"] = False
        btn_cerca_r.config(state="disabled")
        btn_stop_r.config(state="normal")
        threading.Thread(target=esegui_ricerca, daemon=True).start()

    def ferma_ricerca():
        search_state["stop"] = True
        btn_stop_r.config(state="disabled")

    btn_cerca_r.config(command=avvia_ricerca)
    btn_stop_r.config(command=ferma_ricerca)

    def _on_cerca_nuovi_close():
        global cerca_nuovi_win
        ferma_ricerca()
        if detail_state["win"] is not None and detail_state["win"].winfo_exists():
            detail_state["win"].destroy()
        cerca_nuovi_win.destroy()
        cerca_nuovi_win = None

    cerca_nuovi_win.protocol("WM_DELETE_WINDOW", _on_cerca_nuovi_close)

# ------------------------
# -
# UI-----------------------
root = tk.Tk()
root.title("LEGO Smista PRO 🔥 " + version)
root.geometry("1500x950")

ui_settings = load_ui_settings()
try:
    ICON_SIZE = max(90, min(260, int(ui_settings.get("icon_size", ICON_SIZE))))
except Exception:
    ICON_SIZE = 140
try:
    colonne = max(8, min(30, int(ui_settings.get("colonne", colonne))))
except Exception:
    colonne = 18

from tkinter import font

bold_font = font.nametofont("TkDefaultFont").copy()
bold_font.configure(weight="bold")

normal_font = font.nametofont("TkDefaultFont")
aggiorna_font_griglia()

# Stile pulsanti condiviso: usa il font nativo di sistema e una palette coerente.
BUTTON_BG = "#ffd500"          # LEGO yellow
BUTTON_FG = "#111111"
BUTTON_ACTIVE_BG = "#e5bd00"
BUTTON_DISABLED_FG = "#756b42"

button_font = normal_font.copy()
button_font.configure(size=12)
button_font_bold = button_font.copy()
button_font_bold.configure(weight="bold")

root.option_add("*Button.Font", button_font_bold)
root.option_add("*Button.Background", BUTTON_BG)
root.option_add("*Button.Foreground", BUTTON_FG)
root.option_add("*Button.activeBackground", BUTTON_ACTIVE_BG)
root.option_add("*Button.activeForeground", BUTTON_FG)
root.option_add("*Button.disabledForeground", BUTTON_DISABLED_FG)
root.option_add("*Button.relief", "raised")
root.option_add("*Button.borderWidth", 2)
root.option_add("*Button.highlightThickness", 1)
root.option_add("*Button.padX", 10)
root.option_add("*Button.padY", 6)


def stile_pulsante(widget, bg=BUTTON_BG, fg=BUTTON_FG, active_bg=BUTTON_ACTIVE_BG,
                    bold=False, padx=10, pady=7):
    """Applica lo stile comune, lasciando invariati comando e geometria."""
    widget.configure(
        font=button_font_bold if bold else button_font,
        bg=bg,
        fg=fg,
        activebackground=active_bg,
        activeforeground=fg,
        disabledforeground=BUTTON_DISABLED_FG,
        relief="raised",
        overrelief="sunken",
        borderwidth=2,
        highlightthickness=1,
        highlightbackground="#b89a00" if bg == BUTTON_BG else bg,
        padx=padx,
        pady=pady,
        cursor="pointinghand",
    )
    return widget

color_var = tk.StringVar(value="Tutti")
piece_type_var = tk.StringVar(value=ui_settings.get("piece_type", "Tutti"))
solo_mancanti_var = tk.BooleanVar(value=bool(ui_settings.get("solo_mancanti", False)))
lock_var = tk.BooleanVar(value=bool(ui_settings.get("lock", False)))
togli_completi_var = tk.BooleanVar(value=bool(ui_settings.get("togli_completi", False)))
togli_zero_var = tk.BooleanVar(value=bool(ui_settings.get("togli_zero", False)))
saved_sort_mode = ui_settings.get("sort_mode", "Stud")
if saved_sort_mode == "Lunghezza":
    saved_sort_mode = "Stud"
if saved_sort_mode not in ("Stud", "Peso", "Dimensioni", "Quantita"):
    saved_sort_mode = "Stud"
ordine_var = tk.StringVar(value=saved_sort_mode)
weight_filter_var = None
pack_filter_var = None

# 🔹 BARRA ALTA: pulsanti su 2 righe + filtri
frame_top = tk.Frame(root)
frame_top.pack(pady=5, fill="x")

frame_actions = tk.Frame(frame_top)
frame_actions.pack(fill="x", pady=(0, 4))
frame_actions_row1 = tk.Frame(frame_actions)
frame_actions_row1.pack(fill="x", pady=(0, 3))
frame_actions_row2 = tk.Frame(frame_actions)
frame_actions_row2.pack(fill="x")

frame_filters = tk.Frame(frame_top)
frame_filters.pack(fill="x")

entry_set = tk.Entry(frame_actions_row1, width=24, font=("Arial", 12))
entry_set.pack(side="left", padx=2)
entry_set.bind("<KeyRelease>", on_type)
entry_set.bind("<Return>", lambda e: aggiungi_set())

btn_add_set = tk.Button(frame_actions_row1, text="Aggiungi set", command=aggiungi_set)
btn_add_set.pack(side="left", padx=2)
btn_gestione = tk.Button(frame_actions_row1, text="Gestione Set", command=apri_gestione_set)
btn_gestione.pack(side="left", padx=2)
btn_aggiorna = tk.Button(frame_actions_row1, text="Aggiorna lista", command=carica_mysets)
btn_aggiorna.pack(side="left", padx=2)
btn_stato = tk.Button(frame_actions_row1, text="Stato Set", command=apri_riepilogo)
btn_stato.pack(side="left", padx=2)
btn_log = tk.Button(frame_actions_row1, text="Log Pezzi", command=apri_log_pezzi)
btn_log.pack(side="left", padx=2)
btn_stampa_set = tk.Button(frame_actions_row1, text="Stampa Nr+Nomi", command=esporta_foglio_etichette_set_a4)
btn_stampa_set.pack(side="left", padx=2)
btn_batch = tk.Button(frame_actions_row1, text="Batch Colore", command=apri_modalita_batch_colore)
btn_batch.pack(side="left", padx=2)
btn_magic = tk.Button(frame_actions_row1, text="🪄 Magic Sets", command=apri_magic_sets, bg="#7c3aed", fg="white")
btn_magic.pack(side="left", padx=2)
btn_cerca_nuovi = tk.Button(frame_actions_row1, text="🔍 Cerca Nuovi", command=apri_cerca_nuovi_sets, bg="#1565c0", fg="white")
btn_cerca_nuovi.pack(side="left", padx=2)

lbl_colore = tk.Label(frame_filters, text="Colore:")
btn_undo = tk.Button(frame_actions_row2, text="⎌ Undo", command=annulla_ultimo_pezzo)
btn_undo.pack(side="left", padx=2)
btn_qr_master = tk.Button(frame_actions_row2, text="▦ QR iPhone", command=mostra_qr_master, bg="#111827", fg="white")
btn_qr_master.pack(side="left", padx=2)
btn_sorgente = tk.Button(frame_actions_row2, text="◉ Sorgente", command=apri_sorgente_visiva,
                         bg="#6a1b9a", fg="white")
btn_sorgente.pack(side="left", padx=2)
btn_calibra_iphone = tk.Button(frame_actions_row2, text="◎ Calibra",
                               command=calibra_sorgente_visiva,
                               bg="#ef6c00", fg="white")
btn_calibra_iphone.pack(side="left", padx=2)
btn_analizza_iphone = tk.Button(frame_actions_row2, text="⌾ Analizza",
                                command=analizza_sorgente_visiva,
                                bg="#2e7d32", fg="white")
btn_analizza_iphone.pack(side="left", padx=2)

# Le azioni normali condividono lo stesso tono; i colori indicano solo il tipo
# di azione, così la toolbar rimane leggibile senza sembrare disordinata.
for _btn in (btn_add_set, btn_gestione, btn_aggiorna, btn_stato, btn_log,
             btn_stampa_set, btn_batch, btn_undo):
    stile_pulsante(_btn, bold=True)
stile_pulsante(btn_magic, "#e3000b", "#111111", "#b80009", bold=True)
stile_pulsante(btn_cerca_nuovi, "#006cb7", "#111111", "#00548f", bold=True)
stile_pulsante(btn_qr_master, "#aeb8bf", "#111111", "#929da5", bold=True)
stile_pulsante(btn_sorgente, "#8e44ad", "#111111", "#71368a", bold=True)
stile_pulsante(btn_calibra_iphone, "#f47b20", "#111111", "#d9610c", bold=True)
stile_pulsante(btn_analizza_iphone, "#00a650", "#111111", "#007f3d", bold=True)

def aggiorna_controlli_sorgente():
    label = _camera_mode_label()
    btn_sorgente.config(text=f"◉ {label}")
    btn_calibra_iphone.config(text=f"◎ Calibra {label}")
    btn_analizza_iphone.config(text=f"⌾ Analizza {label}")
    btn_qr_master.config(state="normal" if camera_config.get("mode") == "iphone" else "disabled")

aggiorna_controlli_sorgente()

chk_font = font.Font(size=18, weight="bold")
chk_scurisci = tk.Checkbutton(
    frame_actions_row2,
    text="Scurisci completi",
    variable=solo_mancanti_var,
    font=chk_font,
    command=on_filter_checkbox_change
)
chk_scurisci.pack(side="left", padx=(10, 2))
chk_togli_completi = tk.Checkbutton(
    frame_actions_row2,
    text="Togli completi",
    variable=togli_completi_var,
    font=chk_font,
    command=on_filter_checkbox_change
)
chk_togli_completi.pack(side="left", padx=(4, 2))
chk_togli_zero = tk.Checkbutton(
    frame_actions_row2,
    text="Togli a zero",
    variable=togli_zero_var,
    font=chk_font,
    command=on_filter_checkbox_change
)
chk_togli_zero.pack(side="left", padx=(4, 0))

chk_lock = tk.Checkbutton(
    frame_actions_row2,
    text="🔒",
    variable=lock_var,
    font=font.Font(size=20),
    command=on_lock_checkbox_change,
)
chk_lock.pack(side="left", padx=(12, 0))

lbl_colore.pack(side="left", padx=10)

menu_colore = tk.OptionMenu(frame_filters, color_var, *colori_disponibili)
menu_colore.pack(side="left")
lbl_set = tk.Label(frame_filters, text="Set:")
lbl_set.pack(side="left", padx=10)
set_var = tk.StringVar(value="Tutti")
menu_set = tk.OptionMenu(frame_filters, set_var, "Tutti")
menu_set.pack(side="left")
lbl_tipo = tk.Label(frame_filters, text="Tipo:")
lbl_tipo.pack(side="left", padx=(10, 4))
menu_tipo = tk.OptionMenu(
    frame_filters,
    piece_type_var,
    *PIECE_TYPE_OPTIONS,
    command=on_piece_type_filter_change
)
menu_tipo.pack(side="left")
lbl_ordina = tk.Label(frame_filters, text="Ordina:")
lbl_ordina.pack(side="left", padx=(10, 4))
menu_ordine = tk.OptionMenu(
    frame_filters,
    ordine_var,
    "Stud",
    "Peso",
    "Dimensioni",
    "Quantita",
    command=on_sort_mode_change
)
menu_ordine.pack(side="left")
lbl_colonne = tk.Label(frame_filters, text="Icone/riga:")
lbl_colonne.pack(side="left", padx=(10, 4))
colonne_var = tk.StringVar(value=str(colonne))
menu_colonne = tk.OptionMenu(
    frame_filters,
    colonne_var,
    *[str(i) for i in range(8, 31)],
    command=on_colonne_change
)
menu_colonne.pack(side="left")

lbl_grandezza = tk.Label(frame_filters, text="Grandezza:")
lbl_grandezza.pack(side="left", padx=(10, 4))
icon_size_var = tk.StringVar(value=str(ICON_SIZE))
menu_icone = tk.OptionMenu(
    frame_filters,
    icon_size_var,
    *[str(i) for i in range(100, 201, 10)],
    command=on_icon_size_change
)
menu_icone.pack(side="left")
cerca_var = tk.StringVar()  # mantenuto per compatibilità filtri

top_buttons = [
    (btn_add_set, "Aggiungi set", "Aggiungi"),
    (btn_gestione, "Gestione Set", "Gestione"),
    (btn_aggiorna, "Aggiorna lista", "Aggiorna"),
    (btn_stato, "Stato Set", "Stato"),
    (btn_log, "Log Pezzi", "Log"),
    (btn_stampa_set, "Stampa Nr+Nomi", "Stampa"),
    (btn_batch, "Batch Colore", "Batch"),
    (btn_magic, "🪄 Magic Sets", "🪄"),
    (btn_cerca_nuovi, "🔍 Cerca Nuovi", "🔍"),
    (btn_undo, "⎌ Undo", "⎌"),
    (btn_qr_master, "▦ QR iPhone", "▦ QR"),
    (btn_sorgente, "◉ Sorgente visiva", "◉ Sorgente"),
    (btn_calibra_iphone, "◎ Calibra sorgente", "◎ Calibra"),
    (btn_analizza_iphone, "⌾ Analizza sorgente", "⌾ Analizza"),
]

toolbar_resize_job = None
toolbar_state = {
    "compact": None,
    "very_compact": None
}

def aggiorna_toolbar_compatta(_event=None):
    global toolbar_resize_job
    toolbar_resize_job = None
    try:
        w = root.winfo_width()
        compact = w < 1780
        very_compact = w < 1520

        if (
            toolbar_state["compact"] == compact
            and toolbar_state["very_compact"] == very_compact
        ):
            return

        toolbar_state["compact"] = compact
        toolbar_state["very_compact"] = very_compact

        if very_compact:
            entry_set.config(width=14, font=("Arial", 12))
        elif compact:
            entry_set.config(width=18, font=("Arial", 12))
        else:
            entry_set.config(width=24, font=("Arial", 13))

        if compact:
            btn_font = button_font_bold.copy()
            btn_font.configure(size=11)
            pad_btn = 5
            lbl_colore.config(text="Col:")
            lbl_set.config(text="Set:")
            lbl_tipo.config(text="Tipo:")
            lbl_ordina.config(text="Ord:")
            lbl_colonne.config(text="Col:")
            lbl_grandezza.config(text="Dim:")
            chk_scurisci.config(text="Scurisci", font=("Arial", 9))
            chk_togli_completi.config(text="Togli", font=("Arial", 9))
            chk_togli_zero.config(text="No 0", font=("Arial", 9))
            apply_set_menu_width()
            menu_tipo.config(width=8)
            menu_ordine.config(width=8)
            menu_colonne.config(width=5)
            menu_icone.config(width=5)
        else:
            btn_font = button_font_bold.copy()
            btn_font.configure(size=12)
            pad_btn = 7
            lbl_colore.config(text="Colore:")
            lbl_set.config(text="Set:")
            lbl_tipo.config(text="Tipo:")
            lbl_ordina.config(text="Ordina:")
            lbl_colonne.config(text="Icone/riga:")
            lbl_grandezza.config(text="Grandezza:")
            chk_scurisci.config(text="Scurisci Pezzi Completi", font=("Arial", 11))
            chk_togli_completi.config(text="Togli pezzi completi", font=("Arial", 11))
            chk_togli_zero.config(text="Togli pezzi a 0", font=("Arial", 11))
            apply_set_menu_width()
            menu_tipo.config(width=10)
            menu_ordine.config(width=10)
            menu_colonne.config(width=6)
            menu_icone.config(width=6)

        for btn, full_text, compact_text in top_buttons:
            btn.config(
                text=compact_text if compact else full_text,
                font=btn_font,
                padx=6,
                pady=pad_btn
            )

        frame_top.pack_configure(pady=2 if compact else 6)
        frame_actions.pack_configure(pady=(0, 3 if compact else 5))
    except Exception:
        pass

def on_root_configure(_event=None):
    global toolbar_resize_job
    if toolbar_resize_job is not None:
        try:
            root.after_cancel(toolbar_resize_job)
        except Exception:
            pass
    toolbar_resize_job = root.after(80, aggiorna_toolbar_compatta)

root.bind("<Configure>", on_root_configure)
root.bind("<Control-z>", annulla_ultimo_pezzo)
root.after(50, aggiorna_toolbar_compatta)

# 🔹 INFO (come prima)
label_info = tk.Label(root)
label_info.pack()

label_movimento = tk.Label(root, font=("Arial", 20))
label_movimento.pack()

label_progresso_set = tk.Label(root, font=("Arial", 12), fg="#333333")
label_progresso_set.pack()

risultato = tk.Label(root, font=("Arial", 16))
risultato.pack()

# 🔹 GRID SCROLL
canvas = tk.Canvas(root)
scroll = tk.Scrollbar(root)

def on_main_canvas_scroll(*args):
    canvas.yview(*args)
    schedule_refresh_visible_grid()

def on_main_canvas_yscroll(first, last):
    scroll.set(first, last)
    schedule_refresh_visible_grid()

scroll.configure(command=on_main_canvas_scroll)
bind_mousewheel_scroll(canvas, on_scroll=schedule_refresh_visible_grid)

frame_grid = tk.Frame(canvas)

canvas.create_window((0, 0), window=frame_grid, anchor="nw")
canvas.configure(yscrollcommand=on_main_canvas_yscroll)
canvas.bind("<Configure>", lambda e: schedule_refresh_visible_grid())

canvas.pack(side="left", fill="both", expand=True)
scroll.pack(side="right", fill="y")

aggiorna_layout_griglia()
aggiorna_dimensione_root()

carica_mysets()
aggiorna_numerazione_set()
load_disabled_sets()
aggiorna_filtri_set()
ensure_version_backup()
root.after(25, _process_import_ui_requests)
root.after(25, _process_master_ui_requests)
start_master_server()

# 🔥 CLEANUP: flush salvataggi in sospeso quando chiude
def on_close():
    """Salva tutto prima di chiudere"""
    save_lego_data(force=True)
    global save_thread
    if save_thread is not None and save_thread.is_alive():
        save_thread.join(timeout=3)
    save_log(force=True)
    save_ui_settings()
    _close_camera_session()
    if preview_transport is not None:
        preview_transport.stop()
    if master_server is not None:
        master_server.stop()
    root.destroy()

root.protocol("WM_DELETE_WINDOW", on_close)
root.mainloop() 
