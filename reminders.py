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

import os
from datetime import date, datetime, time, timedelta

import notify
import store

# positive minute-offsets only — a negative offset would fire *after* the appt and
# never match the once-only gate, so it's silently dropped here rather than later.
OFFSETS = sorted({int(x) for x in os.environ.get("LIFEPLANNER_REMINDERS", "1440,60").split(",")
                  if x.strip().isdigit()}, reverse=True)
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


def _load_last(now):
    try:
        # always naive (drop any tz offset) so it compares with datetime.now()
        last = datetime.fromisoformat(STATE.read_text("utf-8").strip()).replace(tzinfo=None)
        # clamp to now: a future-dated state (clock jumped back, hand-edited file)
        # would otherwise suppress every reminder forever.
        return min(last, now)
    except (OSError, ValueError):
        return now  # first run: don't fire historical reminders


def _save_last(now):
    try:
        store._ensure()
        # atomic: a crash mid-write must not truncate the state file (which would
        # drop the missed-reminder window). write to .tmp then rename.
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(now.isoformat(timespec="seconds"), "utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, STATE)
    except OSError:
        pass


def main():
    if not notify.configured():
        return
    now = datetime.now().replace(microsecond=0)
    last = _load_last(now)
    start, end = now.date().isoformat(), (now + timedelta(days=HORIZON_DAYS)).date().isoformat()
    ok = True
    for ap in store.list_items("appointments"):
        title = ap.get("title", "appointment")
        for occ in store.occurrences_in(ap, start, end):
            for fire_dt, label, when_text in _fires_for(occ):
                if last < fire_dt <= now:  # became due since the last check → fire once
                    try:
                        notify.send(title, f"{label} · {when_text}",
                                    priority=4, tags=["alarm_clock"], view="appointments")
                    except OSError:  # urllib URLError → ntfy/network down
                        ok = False   # don't advance state; retry the window next run
    if ok:  # only advance past reminders we actually delivered → never miss one
        _save_last(now)


if __name__ == "__main__":
    main()
