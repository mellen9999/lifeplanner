#!/usr/bin/env python3
"""lifeplanner nudges — the forcing function.

a planner you have to remember to open is just a todo list. this reaches out:
a daily standup and a weekly review pushed to your phone (via ntfy), with
overdue alerts that escalate the longer you ignore them — 1-2d normal, 3-6d
high, 7d+ urgent (bypasses do-not-disturb). meant to run on a timer (e.g. every
15 min); stateful, so each nudge fires at most once per day / per week.

config via env (no ntfy server/topic → does nothing, so it's safe/optional):
  LIFEPLANNER_NTFY_SERVER / LIFEPLANNER_NTFY_TOPIC   (see notify.py)
  LIFEPLANNER_STANDUP_HOUR   hour 0-23 the daily standup may fire (default 8)
  LIFEPLANNER_REVIEW_DOW     weekday for the weekly review, mon=0 (default 6 = sun)
  LIFEPLANNER_REVIEW_HOUR    hour the weekly review may fire (default 18)
  LIFEPLANNER_JOURNAL_HOUR   hour the nightly "write your diary" prompt may fire
                             (default 21); skipped on days you've already written one
  LIFEPLANNER_NUDGE          set to 0/off/false to disable entirely
"""

import json
import os
from datetime import datetime

import notify
import review
import store

STATE = store.DATA / "nudge_last.json"


def _int_env(name, default, lo, hi):
    try:
        return max(lo, min(hi, int(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


STANDUP_HOUR = _int_env("LIFEPLANNER_STANDUP_HOUR", 8, 0, 23)
REVIEW_DOW = _int_env("LIFEPLANNER_REVIEW_DOW", 6, 0, 6)
REVIEW_HOUR = _int_env("LIFEPLANNER_REVIEW_HOUR", 18, 0, 23)
JOURNAL_HOUR = _int_env("LIFEPLANNER_JOURNAL_HOUR", 21, 0, 23)


def _enabled():
    if os.environ.get("LIFEPLANNER_NUDGE", "").lower() in ("0", "off", "false", "no"):
        return False
    return notify.configured()


def _load_state():
    try:
        s = json.loads(STATE.read_text("utf-8"))
        return s if isinstance(s, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state):
    try:
        store._ensure()
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), "utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, STATE)
    except OSError:
        pass


# ---- message building (pure) ------------------------------------------------

def _standup_alert(overdue):
    """(priority, tags) for the standup — escalates with the worst overdue item.
    ntfy priority 5 is urgent and bypasses do-not-disturb; that's the 7-day teeth."""
    if not overdue:
        return 3, ["sunrise"]
    worst = overdue[0]["days_late"]
    if worst >= 7:
        return 5, ["rotating_light"]
    if worst >= 3:
        return 4, ["warning"]
    return 3, ["sunrise"]


def standup_text(slip):
    """terse standup body from a whats_slipping() dict, or '' if nothing to say
    (no nagging on a clean day)."""
    parts = []
    od = slip.get("overdue_todos") or []
    if od:
        names = ", ".join(f'{t["title"]} {t["days_late"]}d' for t in od[:3])
        parts.append(f"{len(od)} overdue: {names}")
    stale = slip.get("stale_todos") or []
    if stale:
        parts.append(f"{len(stale)} stale")
    # only a *real* gap (you've logged before, then lapsed) is worth a push — a
    # brand-new planner with no wins shouldn't get nagged daily.
    gap = slip.get("days_since_win")
    if gap is not None and gap >= 2:
        parts.append(f"{gap}d since a win")
    routines = slip.get("routines_today") or []
    if routines:
        names = ", ".join(r["title"] for r in routines[:4])
        more = f" +{len(routines) - 4}" if len(routines) > 4 else ""
        parts.append(f"routines left: {names}{more}")
    today = slip.get("today")
    appts = [x["title"] for x in (slip.get("next_load") or [])
             if x.get("date") == today and x.get("kind") == "appointment"]
    if appts:
        parts.append("today: " + ", ".join(appts))
    return "\n".join(parts)


def review_text(rv):
    """terse weekly-review body from a review() dict."""
    rate = rv.get("completion_rate")
    pct = f"{round(rate * 100)}%" if rate is not None else "—"
    parts = [f'week: {pct} done ({rv.get("completed_due", 0)}/{rv.get("due_in_window", 0)})'
             f' · {rv.get("wins_count", 0)} wins']
    sl = rv.get("slipped_todos") or []
    if sl:
        names = ", ".join(t["title"] for t in sl[:3])
        parts.append(f"{len(sl)} still open: {names}")
    rt = rv.get("routine_total", 0)
    if rt:
        parts.append(f'routines: {rv.get("routine_completions", 0)}/{rt}')
        # call out the worst-held routine so it's actionable, not just a number
        worst = next(iter(rv.get("routines") or []), None)
        if worst and worst["done"] / worst["total"] < 0.6:
            parts.append(f'slipping: {worst["title"]} {worst["done"]}/{worst["total"]}')
    parts.append("open lifeplanner — plan the week")
    return "\n".join(parts)


# ---- driver -----------------------------------------------------------------

def main(now=None):
    if not _enabled():
        return
    now = now or datetime.now()
    today = now.date().isoformat()
    state = _load_state()
    changed = False

    # daily standup: once per day, at/after the configured hour.
    if now.hour >= STANDUP_HOUR and state.get("standup") != today:
        slip = review.whats_slipping()
        text = standup_text(slip)
        if text:
            priority, tags = _standup_alert(slip.get("overdue_todos") or [])
            try:
                notify.send("standup", text, priority=priority, tags=tags, view="todos")
                state["standup"], changed = today, True
            except OSError:
                pass  # ntfy down → retry next run, don't mark fired
        else:
            state["standup"], changed = today, True  # clean day → no nag, but don't recheck

    # nightly diary prompt: once per day at/after its hour — but only if nothing's
    # been journaled today, so a day you've already written gets no nag. tapping the
    # push opens the journal view to write. this is the forcing function for the diary.
    if now.hour >= JOURNAL_HOUR and state.get("journal") != today:
        wrote_today = any(j.get("when", "")[:10] == today for j in store.list_items("journal"))
        if wrote_today:
            state["journal"], changed = today, True  # already written → no prompt
        else:
            try:
                notify.send("journal", "what happened today? tap to write it down.",
                            priority=3, tags=["memo"], view="journal")
                state["journal"], changed = today, True
            except OSError:
                pass  # ntfy down → retry next run, don't mark fired

    # weekly review: once on the configured weekday, at/after its hour.
    if (now.weekday() == REVIEW_DOW and now.hour >= REVIEW_HOUR
            and state.get("review") != today):
        try:
            notify.send("weekly review", review_text(review.review(7)),
                        priority=4, tags=["calendar"], view="achievements")
            state["review"], changed = today, True
        except OSError:
            pass

    if changed:
        _save_state(state)


if __name__ == "__main__":
    main()
