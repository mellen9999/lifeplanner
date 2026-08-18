#!/usr/bin/env python3
"""shared ntfy push for lifeplanner — the one place the reminder + nudge senders
publish through, so priority/click/auth handling never drifts between them.

config via env (no server/topic → configured() is false, so every caller no-ops
and the app stays safe/optional):
  LIFEPLANNER_NTFY_SERVER       base url (e.g. http://127.0.0.1:2587)
  LIFEPLANNER_NTFY_TOPIC        topic to publish to
  LIFEPLANNER_NTFY_ALARM_TOPIC  optional separate topic for appointment alarms, so
                                the phone can give them a distinct loud sound / DND
                                override. falls back to the main topic if unset.
  LIFEPLANNER_URL               optional app url; tapping a push opens it
"""

import json
import os
import urllib.request

# read at call time, never at import: a module that pulls this one in early
# (a shared helper, a test) must not be able to freeze an empty config.

def _env(name, strip_slash=False):
    v = os.environ.get(name, "").strip()
    return v.rstrip("/") if strip_slash else v


def server():
    return _env("LIFEPLANNER_NTFY_SERVER", strip_slash=True)


def topic():
    return _env("LIFEPLANNER_NTFY_TOPIC")


def configured():
    return bool(server() and topic())


def alarm_topic():
    """topic for wake-you-up appointment alarms — its own when configured, else the
    normal topic (alarms still fire, just without a distinct sound)."""
    return _env("LIFEPLANNER_NTFY_ALARM_TOPIC") or topic()


def send(title, message, priority=4, tags=None, click="", view="", topic=""):
    """publish one ntfy message. returns False if not configured; raises OSError
    (urllib URLError) when the server is unreachable, so a caller can choose not
    to advance its once-only state and retry the window next run."""
    if not configured():
        return False
    payload = {"topic": topic or _env("LIFEPLANNER_NTFY_TOPIC"), "title": title, "message": message,
               "priority": int(priority)}
    if tags:
        payload["tags"] = list(tags)
    link = click or (_env("LIFEPLANNER_URL") + ("#" + view if view else ""))
    if link:
        payload["click"] = link
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(server() + "/", data=body)
    urllib.request.urlopen(req, timeout=10).read()
    return True
