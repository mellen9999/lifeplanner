"""http-level tests — the parts unit tests can't see: request framing on a
keep-alive connection, auth, and the coach routes end to end.

the app runs as a real subprocess on an ephemeral port with its own data dir;
nothing here touches the network beyond localhost.

run:  python3 -m unittest discover -s tests
"""

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = tempfile.mkdtemp(prefix="lp-api-test-")
        cls.port = _free_port()
        # a missing claude binary keeps the dismissal's background re-ask hermetic:
        # it fails fast and writes nothing instead of spawning a real model run.
        env = {**os.environ, "LIFEPLANNER_DATA": cls.data, "LIFEPLANNER_CALDAV": "0",
               "LIFEPLANNER_PORT": str(cls.port), "LIFEPLANNER_NO_BROWSER": "1",
               "LIFEPLANNER_CLAUDE_BIN": "/nonexistent/claude"}
        cls.srv = subprocess.Popen([sys.executable, str(ROOT / "app.pyw")], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        token = Path(cls.data) / "token"
        for _ in range(100):  # wait for the bind + token, then bail loudly
            if token.exists():
                probe = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=1)
                try:
                    probe.request("GET", "/")
                    break
                except OSError:
                    pass
                finally:
                    probe.close()
            time.sleep(0.05)
        else:
            cls.srv.terminate()
            raise AssertionError("server never came up")
        cls.token = token.read_text().strip()

    @classmethod
    def tearDownClass(cls):
        cls.srv.terminate()
        cls.srv.wait(timeout=5)

    def conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)

    def call(self, conn, method, path, body=None):
        headers = {"Authorization": "Bearer " + self.token}
        if body is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        r = conn.getresponse()
        return r.status, r.read()

    def test_a_post_body_never_desyncs_the_next_request(self):
        # every POST must consume its body: on keep-alive an unread one is read
        # as the next request line, and the following call comes back 501.
        conn = self.conn()
        try:
            for path in ("/api/coach/dismiss", "/api/todos/reorder"):
                self.call(conn, "POST", path, {"unused": True})
                status, body = self.call(conn, "GET", "/api/state")
                self.assertEqual(status, 200, f"{path} desynced the connection")
                self.assertIn("coach", json.loads(body))
        finally:
            conn.close()

    def test_malformed_body_is_a_clean_400(self):
        conn = self.conn()
        try:
            conn.request("POST", "/api/todos", "{not json",
                         {"Authorization": "Bearer " + self.token,
                          "Content-Type": "application/json"})
            self.assertEqual(conn.getresponse().status, 400)
        finally:
            conn.close()

    def test_the_api_is_shut_without_the_token(self):
        conn = self.conn()
        try:
            conn.request("GET", "/api/state")
            self.assertEqual(conn.getresponse().status, 401)
        finally:
            conn.close()

    def set_line(self, line):
        """write a directive the way the timer would — straight into the data dir,
        so this test stays about the http surface (store's own module state is
        pinned to whichever data dir imported it first)."""
        p = Path(self.data) / "coach.json"
        p.write_text(json.dumps({"line": line, "created": "2026-08-18T09:00", "fp": "fp"}))

    def test_dismiss_hides_the_line_and_a_new_one_brings_it_back(self):
        self.set_line("ship the auth flow")
        conn = self.conn()
        try:
            _, body = self.call(conn, "GET", "/api/state")
            self.assertEqual(json.loads(body)["coach"]["line"], "ship the auth flow")
            status, body = self.call(conn, "POST", "/api/coach/dismiss", {})
            self.assertEqual((status, json.loads(body)), (200, {"dismissed": True}))
            _, body = self.call(conn, "GET", "/api/state")
            self.assertEqual(json.loads(body)["coach"]["line"], "")
            self.set_line("call the landlord")
            _, body = self.call(conn, "GET", "/api/state")
            self.assertEqual(json.loads(body)["coach"]["line"], "call the landlord")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
