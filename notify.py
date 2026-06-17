#!/usr/bin/env python3
"""shared ntfy push for lifeplanner — the one place the reminder + nudge senders
publish through, so priority/click/auth handling never drifts between them.

config via env (no server/topic → configured() is false, so every caller no-ops
and the app stays safe/optional):
  LIFEPLANNER_NTFY_SERVER  base url (e.g. http://127.0.0.1:2587)
  LIFEPLANNER_NTFY_TOPIC   topic to publish to
  LIFEPLANNER_URL          optional app url; tapping a push opens it
"""

import json
import os
import urllib.request

SERVER = os.environ.get("LIFEPLANNER_NTFY_SERVER", "").strip().rstrip("/")
TOPIC = os.environ.get("LIFEPLANNER_NTFY_TOPIC", "").strip()
APP_URL = os.environ.get("LIFEPLANNER_URL", "").strip()


def configured():
    return bool(SERVER and TOPIC)


def send(title, message, priority=4, tags=None, click="", view=""):
    """publish one ntfy message. returns False if not configured; raises OSError
    (urllib URLError) when the server is unreachable, so a caller can choose not
    to advance its once-only state and retry the window next run."""
    if not configured():
        return False
    payload = {"topic": TOPIC, "title": title, "message": message,
               "priority": int(priority)}
    if tags:
        payload["tags"] = list(tags)
    link = click or (APP_URL + ("#" + view if view else ""))
    if link:
        payload["click"] = link
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(SERVER + "/", data=body)
    urllib.request.urlopen(req, timeout=10).read()
    return True
