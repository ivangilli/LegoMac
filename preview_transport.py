"""Trasporto stabile per l'anteprima iPhone: WebSocket + Bonjour."""

from __future__ import annotations

import json
import socket
import threading
import time


class PreviewTransport:
    def __init__(self, pin, http_port, frame_callback, ws_port=None):
        self.pin = str(pin)
        self.http_port = int(http_port)
        self.ws_port = int(ws_port or (self.http_port + 1))
        self.frame_callback = frame_callback
        self.stop_event = threading.Event()
        self.thread = None
        self.server = None
        self.zeroconf = None
        self.service_info = None
        self.last_seen = 0.0
        self.last_sequence = -1
        self.device_name = ""
        self.frames_received = 0

    def start(self):
        self.thread = threading.Thread(target=self._run, name="LegoPreviewWebSocket", daemon=True)
        self.thread.start()
        self._register_bonjour()
        return self.ws_port

    def _run(self):
        try:
            from websockets.sync.server import serve
        except ImportError:
            print("[PREVIEW] WebSocket non installato; resta attivo il fallback HTTP.")
            return

        def handler(websocket):
            try:
                raw = websocket.recv(timeout=6)
                hello = json.loads(raw) if isinstance(raw, str) else {}
                if hello.get("type") != "hello" or str(hello.get("pin", "")) != self.pin:
                    websocket.close(code=1008, reason="PIN non valido")
                    return
                self.device_name = str(hello.get("device", "iPhone"))[:80]
                self.last_seen = time.time()
                websocket.send(json.dumps({
                    "type": "ready", "heartbeat": 2, "max_fps": 8,
                    "jpeg_quality": 0.62,
                }))
                while not self.stop_event.is_set():
                    try:
                        message = websocket.recv(timeout=5)
                    except TimeoutError:
                        websocket.send(json.dumps({"type": "ping", "time": time.time()}))
                        continue
                    self.last_seen = time.time()
                    if isinstance(message, bytes):
                        if 100 <= len(message) <= 1_500_000 and message.startswith(b"\xff\xd8"):
                            self.frames_received += 1
                            self.frame_callback(message, self.frames_received, self.device_name)
                        continue
                    payload = json.loads(message)
                    if payload.get("type") == "frame":
                        sequence = int(payload.get("sequence", -1))
                        if sequence <= self.last_sequence:
                            websocket.send(json.dumps({"type": "drop", "sequence": sequence}))
                        else:
                            self.last_sequence = sequence
                    elif payload.get("type") == "ping":
                        websocket.send(json.dumps({"type": "pong", "time": time.time()}))
            except Exception as exc:
                print(f"[PREVIEW] Connessione chiusa: {exc}")

        try:
            with serve(handler, "0.0.0.0", self.ws_port, max_size=1_500_000,
                       ping_interval=2, ping_timeout=6, compression=None) as server:
                self.server = server
                print(f"[PREVIEW] WebSocket attivo sulla porta {self.ws_port}")
                server.serve_forever()
        except Exception as exc:
            print(f"[PREVIEW] WebSocket non avviato: {exc}")

    def _register_bonjour(self):
        try:
            from zeroconf import ServiceInfo, Zeroconf
            host = socket.gethostname().split(".")[0] or "Mac"
            address = socket.gethostbyname(socket.gethostname())
            self.service_info = ServiceInfo(
                "_legovision._tcp.local.", f"LEGO MASTER {host}._legovision._tcp.local.",
                addresses=[socket.inet_aton(address)], port=self.http_port,
                properties={"ws_port": str(self.ws_port), "version": "12.2"},
                server=f"{socket.gethostname().rstrip('.')}.",
            )
            self.zeroconf = Zeroconf()
            self.zeroconf.register_service(self.service_info)
            print("[PREVIEW] MASTER pubblicata con Bonjour")
        except Exception as exc:
            print(f"[PREVIEW] Bonjour non disponibile: {exc}")

    def stop(self):
        self.stop_event.set()
        if self.server is not None:
            try:
                self.server.shutdown()
            except Exception:
                pass
        if self.zeroconf is not None and self.service_info is not None:
            try:
                self.zeroconf.unregister_service(self.service_info)
                self.zeroconf.close()
            except Exception:
                pass

