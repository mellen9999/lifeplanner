#!/usr/bin/env python3
"""lifeplanner reminders — pushes "1 day / 1 hour before" alerts for upcoming
appointments to an ntfy topic. meant to run on a timer (e.g. every 5 min);
stateful, so each reminder fires exactly once.

configured via env (no config → does nothing, so it's safe/optional):
  LIFEPLANNER_NTFY_SERVER  base url of the ntfy server (e.g. http://127.0.0.1:2587)
  LIFEPLANNER_NTFY_TOPIC   topic to publish to
  LIFEPLANNER_REMINDERS    offsets in minutes before a *timed* appt (default "1440,60")

reminders fire for the next occurrence of each appointment (recurrence expanded).
timed appts → at each offset before the time; all-day appts → evening before + morning of.
"""

import json
import os
import urllib.request
from datetime import date, datetime, time, timedelta

import store

SERVER = os.environ.get("LIFEPLANNER_NTFY_SERVER", "").strip().rstrip("/")
TOPIC = os.environ.get("LIFEPLANNER_NTFY_TOPIC", "").strip()
OFFSETS = sorted({int(x) for x in os.environ.get("LIFEPLANNER_REMINDERS", "1440,60").split(",")
                  if x.strip().lstrip("-").isdigit()}, reverse=True)
STATE = store.DATA / "reminders_last.txt"
HORIZON_DAYS = 2  # how far ahead to look (covers the 1-day-before reminder)


def _label(off_min):
    if off_min % 1440 == 0:
        n = off_min // 1440
        return f"in {n} day" + ("s" if n > 1 else "")
    if off_min % 60 == 0:
        n = off_min // 60
        return f"in {n} hour" + ("s" if n > 1 else "")
    return f"in {off_min} min"


def _fires_for(occ_when):
    """yield (fire_datetime, label, when_text) for one appointment occurrence."""
    if len(occ_when) > 10:  # timed
        appt_dt = datetime.fromisoformat(occ_when)
        when_text = appt_dt.strftime("%a %b %-d, %-I:%M %p").lower()
        for off in OFFSETS:
            yield appt_dt - timedelta(minutes=off), _label(off), when_text
    else:  # all-day
        d = date.fromisoformat(occ_when)
        when_text = d.strftime("%a %b %-d").lower()
        yield datetime.combine(d - timedelta(days=1), time(18, 0)), "tomorrow", when_text
        yield datetime.combine(d, time(8, 0)), "today", when_text


def _push(title, message):
    body = json.dumps({
        "topic": TOPIC, "title": title, "message": message,
        "priority": 4, "tags": ["alarm_clock"],
    }).encode("utf-8")
    req = urllib.request.Request(SERVER + "/", data=body)
    urllib.request.urlopen(req, timeout=10).read()


def _load_last(now):
    try:
        return datetime.fromisoformat(STATE.read_text("utf-8").strip())
    except (OSError, ValueError):
        return now  # first run: don't fire historical reminders


def _save_last(now):
    try:
        store._ensure()
        STATE.write_text(now.isoformat(timespec="seconds"), "utf-8")
        STATE.chmod(0o600)
    except OSError:
        pass


def main():
    if not (SERVER and TOPIC):
        return
    now = datetime.now().replace(microsecond=0)
    last = _load_last(now)
    start, end = now.date().isoformat(), (now + timedelta(days=HORIZON_DAYS)).date().isoformat()
    for ap in store.list_items("appointments"):
        title = ap.get("title", "appointment")
        for occ in store.occurrences_in(ap, start, end):
            for fire_dt, label, when_text in _fires_for(occ):
                if last < fire_dt <= now:  # became due since the last check → fire once
                    _push(title, f"{label} · {when_text}")
    _save_last(now)


if __name__ == "__main__":
    main()
