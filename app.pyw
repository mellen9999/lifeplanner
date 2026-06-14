#!/usr/bin/env python3
"""lifeplanner web server — stdlib only.

serves the vanilla ui + a small json rest api over the shared store.
single-instance: binds 127.0.0.1:PORT; if that fails an instance is already up,
so we just open the browser to it and exit. localhost-only = no lan exposure.
"""

import hmac
import json
import os
import secrets
import socket
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import store

# all configurable for portability; safe localhost defaults.
HOST = os.environ.get("LIFEPLANNER_HOST", "127.0.0.1")
PORT = int(os.environ.get("LIFEPLANNER_PORT", "8765"))
MAX_BODY = 1 << 20  # 1 MiB request-body cap — a local single-user app never needs more
BASE = Path(__file__).resolve().parent
WEB = BASE / "web"
URL = f"http://{HOST}:{PORT}/"


def _load_token():
    """static bearer token, persisted in the (gitignored) data dir.

    localhost-bind already blocks the network; this gate stops *other origins* —
    a malicious page can fire a cross-origin POST at 127.0.0.1, but can't read
    index.html (same-origin policy) to learn the token, and the custom auth
    header forces a preflight we never approve. generated once, 0600.
    """
    store.DATA.mkdir(exist_ok=True)
    tf = store.DATA / "token"
    if tf.exists():
        t = tf.read_text().strip()
        if t:
            return t
    t = secrets.token_urlsafe(32)
    tf.write_text(t)
    try:
        tf.chmod(0o600)
    except OSError:
        pass
    return t


TOKEN = _load_token()

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".ics": "text/calendar; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webmanifest": "application/manifest+json",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- helpers --------------------------------------------------------------

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", disposition=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        # defense-in-depth: no sniffing, no framing, no referrer leak, and a CSP
        # that matches the app (all first-party files, no inline/eval, no embeds).
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; base-uri 'none'; "
                         "form-action 'self'; frame-ancestors 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            return None  # malformed Content-Length → treated as bad request
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

    def _authed(self):
        """gate the data surfaces; static assets stay open (see _load_token)."""
        u = urlparse(self.path)
        bearer = hmac.compare_digest(self.headers.get("Authorization", ""),
                                     f"Bearer {TOKEN}")
        if u.path.startswith("/api/"):
            return bearer
        if u.path == "/lifeplanner.ics":
            # calendar clients can't set headers — accept ?token= too
            return bearer or hmac.compare_digest(
                parse_qs(u.query).get("token", [""])[0], TOKEN)
        return True

    # -- routing --------------------------------------------------------------

    def do_GET(self):
        if not self._authed():
            return self._json(401, {"error": "unauthorized"})
        path = urlparse(self.path).path
        if path == "/api/state":
            return self._json(200, store.state())
        if path == "/api/version":
            return self._json(200, {"version": store.version()})
        if path == "/api/settings":
            return self._json(200, store.get_settings())
        if path == "/api/export":
            disp = 'attachment; filename="lifeplanner-export.zip"'
            # HEAD must not build the zip (it takes the lock + reads every file)
            # just to discard the body — answer headers only.
            if self.command == "HEAD":
                return self._send(200, b"", "application/zip", disposition=disp)
            return self._send(200, store.export_bytes(), "application/zip", disposition=disp)
        if path == "/lifeplanner.ics":
            return self._send(200, store.build_ics(), CONTENT_TYPES[".ics"])
        if path.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        return self._static(path)

    do_HEAD = do_GET

    def do_POST(self):
        if not self._authed():
            return self._json(401, {"error": "unauthorized"})
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
        if not self._authed():
            return self._json(401, {"error": "unauthorized"})
        if urlparse(self.path).path == "/api/settings":
            data = self._body()
            if data is None:
                return self._json(400, {"error": "bad json"})
            return self._json(200, store.put_settings(data))
        return self._json(404, {"error": "not found"})

    def do_PATCH(self):
        if not self._authed():
            return self._json(401, {"error": "unauthorized"})
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
        if not self._authed():
            return self._json(401, {"error": "unauthorized"})
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
        data = target.read_bytes()
        if target.name == "index.html":
            # hand the same-origin app its token; cross-origin pages can't read this
            tag = f'<meta name="lp-token" content="{TOKEN}">\n</head>'.encode()
            data = data.replace(b"</head>", tag, 1)
        self._send(200, data, ctype)


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


# set LIFEPLANNER_NO_BROWSER=1 to never auto-open a browser tab (e.g. when the
# server runs persistently / is restarted often — you open it from a bookmark).
OPEN_BROWSER = os.environ.get("LIFEPLANNER_NO_BROWSER", "") not in ("1", "true", "yes")


def _open():
    if OPEN_BROWSER:
        webbrowser.open(URL)


def main():
    if already_running():
        _open()
        return
    try:
        httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        # lost the race — someone bound it between our check and now
        _open()
        return
    store.regen_ics()  # ensure feed exists on first boot
    threading.Timer(0.6, _open).start()
    print(f"lifeplanner running at {URL}  (ctrl-c to stop)", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
