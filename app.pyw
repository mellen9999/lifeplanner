#!/usr/bin/env python3
"""lifeplanner web server — stdlib only.

serves the vanilla ui + a small json rest api over the shared store.
single-instance: binds 127.0.0.1:PORT; if that fails an instance is already up,
so we just open the browser to it and exit. localhost-only = no lan exposure.
"""

import json
import os
import socket
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import store

# all configurable for portability; safe localhost defaults.
HOST = os.environ.get("LIFEPLANNER_HOST", "127.0.0.1")
PORT = int(os.environ.get("LIFEPLANNER_PORT", "8765"))
MAX_BODY = 1 << 20  # 1 MiB request-body cap — a local single-user app never needs more
BASE = Path(__file__).resolve().parent
WEB = BASE / "web"
URL = f"http://{HOST}:{PORT}/"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".ics": "text/calendar; charset=utf-8",
    ".svg": "image/svg+xml",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- helpers --------------------------------------------------------------

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        if n > MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def log_message(self, *a):
        pass  # quiet

    # -- routing --------------------------------------------------------------

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/state":
            return self._json(200, store.state())
        if path == "/api/version":
            return self._json(200, {"version": store.version()})
        if path == "/api/settings":
            return self._json(200, store.get_settings())
        if path == "/lifeplanner.ics":
            return self._send(200, store.build_ics(), CONTENT_TYPES[".ics"])
        if path.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        return self._static(path)

    do_HEAD = do_GET

    def do_POST(self):
        path = urlparse(self.path).path
        entity = self._entity(path, exact=True)
        if entity is None:
            return self._json(404, {"error": "not found"})
        data = self._body()
        if data is None:
            return self._json(400, {"error": "bad json"})
        try:
            return self._json(201, store.add_item(entity, data))
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        except store.SyncError:
            return self._json(503, {"error": "calendar server unreachable — not saved"})

    def do_PUT(self):
        if urlparse(self.path).path == "/api/settings":
            data = self._body()
            if data is None:
                return self._json(400, {"error": "bad json"})
            return self._json(200, store.put_settings(data))
        return self._json(404, {"error": "not found"})

    def do_PATCH(self):
        entity, item_id = self._entity_id(urlparse(self.path).path)
        if entity is None:
            return self._json(404, {"error": "not found"})
        data = self._body()
        if data is None:
            return self._json(400, {"error": "bad json"})
        try:
            item = store.update_item(entity, item_id, data)
        except store.SyncError:
            return self._json(503, {"error": "calendar server unreachable — not saved"})
        return self._json(200, item) if item else self._json(404, {"error": "not found"})

    def do_DELETE(self):
        entity, item_id = self._entity_id(urlparse(self.path).path)
        if entity is None:
            return self._json(404, {"error": "not found"})
        try:
            ok = store.delete_item(entity, item_id)
        except store.SyncError:
            return self._json(503, {"error": "calendar server unreachable — not deleted"})
        return self._json(200, {"deleted": ok}) if ok else self._json(404, {"error": "not found"})

    # -- path parsing ---------------------------------------------------------

    def _entity(self, path, exact=False):
        parts = path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "api" and parts[1] in store.ENTITIES:
            return parts[1]
        return None

    def _entity_id(self, path):
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "api" and parts[1] in store.ENTITIES:
            return parts[1], parts[2]
        return None, None

    def _static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB / rel).resolve()
        # path-traversal guard: must stay inside web/
        if WEB not in target.parents and target != WEB:
            return self._send(403, "forbidden", "text/plain")
        if not target.is_file():
            return self._send(404, "not found", "text/plain")
        ctype = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)


def already_running():
    """true if something already holds the port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.connect((HOST, PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def main():
    if already_running():
        webbrowser.open(URL)
        return
    try:
        httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        # lost the race — someone bound it between our check and now
        webbrowser.open(URL)
        return
    store.regen_ics()  # ensure feed exists on first boot
    threading.Timer(0.6, lambda: webbrowser.open(URL)).start()
    print(f"lifeplanner running at {URL}  (ctrl-c to stop)", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
