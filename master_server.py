"""Server LAN leggero per LEGO Smista PRO, basato solo sulla libreria standard."""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def local_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


class MasterServer:
    def __init__(self, snapshot_callback, action_callback, pin, port=8765):
        self.snapshot_callback = snapshot_callback
        self.action_callback = action_callback
        self.pin = str(pin)
        self.port = int(port)
        self.httpd = None
        self.thread = None
        self.preview_lock = threading.Lock()
        self.preview_jpeg = None
        self.preview_updated_at = 0.0
        self.preview_sequence = 0
        self.preview_source = ""

    def set_preview(self, jpeg, sequence=0, source="iPhone"):
        with self.preview_lock:
            self.preview_jpeg = jpeg
            self.preview_updated_at = time.time()
            self.preview_sequence = int(sequence)
            self.preview_source = str(source)

    @property
    def address(self):
        return f"http://{local_ip()}:{self.port}"

    def start(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "LegoMaster/1.0"

            def log_message(self, fmt, *args):
                print("[MASTER] " + (fmt % args))

            def _json(self, status, payload):
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Master-PIN")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.end_headers()
                self.wfile.write(raw)

            def _bytes(self, status, raw, content_type):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.end_headers()
                self.wfile.write(raw)

            def _authorized(self):
                return self.headers.get("X-Master-PIN", "") == owner.pin

            def do_OPTIONS(self):
                self._json(200, {"ok": True})

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/api/status":
                    try:
                        data = owner.snapshot_callback("status", {})
                        self._json(200, {"ok": True, **data})
                    except Exception as exc:
                        self._json(503, {"ok": False, "error": str(exc)})
                    return
                if not self._authorized():
                    self._json(401, {"ok": False, "error": "PIN MASTER non valido"})
                    return
                if parsed.path == "/api/preview":
                    with owner.preview_lock:
                        jpeg = owner.preview_jpeg
                    if jpeg is None:
                        self._json(404, {"ok": False, "error": "Anteprima iPhone non ancora disponibile"})
                    else:
                        self._bytes(200, jpeg, "image/jpeg")
                    return
                query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
                route = {"/api/sets": "sets", "/api/pieces": "pieces",
                         "/api/commands": "commands"}.get(parsed.path)
                if route is None:
                    self._json(404, {"ok": False, "error": "Percorso non trovato"})
                    return
                try:
                    self._json(200, {"ok": True, **owner.snapshot_callback(route, query)})
                except Exception as exc:
                    self._json(500, {"ok": False, "error": str(exc)})

            def do_POST(self):
                if not self._authorized():
                    self._json(401, {"ok": False, "error": "PIN MASTER non valido"})
                    return
                if self.path == "/api/preview":
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                        if length < 100 or length > 1_500_000:
                            raise ValueError("Dimensione JPEG anteprima non valida")
                        jpeg = self.rfile.read(length)
                        if not jpeg.startswith(b"\xff\xd8"):
                            raise ValueError("Il frame non è JPEG")
                        owner.set_preview(jpeg, owner.preview_sequence + 1, "iPhone HTTP")
                        self._json(200, {"ok": True})
                    except Exception as exc:
                        self._json(400, {"ok": False, "error": str(exc)})
                    return
                if self.path != "/api/action":
                    self._json(404, {"ok": False, "error": "Percorso non trovato"})
                    return
                try:
                    length = min(int(self.headers.get("Content-Length", "0")), 100000)
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    result = owner.action_callback(payload)
                    self._json(200 if result.get("ok") else 400, result)
                except Exception as exc:
                    self._json(400, {"ok": False, "error": str(exc)})

        self.httpd = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="LegoMasterServer", daemon=True)
        self.thread.start()
        return self.address

    def stop(self):
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
