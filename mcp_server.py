#!/usr/bin/env python3
"""lifeplanner MCP server — claude's door into the same local store.

reads fresh on every call (no cache), so the user's ui edits are always visible.
needs the official sdk:  pip install mcp
"""

import sys
from datetime import date, timedelta

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    sys.stderr.write("lifeplanner mcp needs the sdk:  pip install mcp\n")
    raise

import store
import review as review_mod

mcp = FastMCP("lifeplanner")


def _upcoming(appts, today):
    """appointments annotated with their next occurrence on/after `today`,
    soonest first — recurring series resolve to their next hit, not the anchor."""
    out = []
    for a in appts:
        nxt = store.next_occurrence(a, today)
        if nxt:
            out.append({**a, "when": nxt})
    return sorted(out, key=lambda a: a["when"])


def _recur(recur, interval, until=""):
    """build a recur rule from loose tool args, or '' for one-time."""
    if not recur:
        return ""
    r = {"freq": recur, "interval": interval}
    u = (until or "").strip()
    if u:
        r["until"] = u
    return r


def _current_recur(item_id):
    """the appointment's stored recur dict (or {} if none) — lets an edit preserve
    a field, like the end-date, that the caller didn't mention."""
    a = next((x for x in store.list_items("appointments") if x.get("id") == item_id), None)
    r = a.get("recur") if a else None
    return r if isinstance(r, dict) else {}


# ---- planning partner -------------------------------------------------------

@mcp.tool()
def whats_slipping() -> dict:
    """what needs attention right now: overdue todos (most late first), stale
    undated todos, days since the last logged win, and the next 2 days' load.
    OPEN every check-in with this — surface what's slipping before anything else,
    then help the user act on it (reschedule, drop, or do it)."""
    return review_mod.whats_slipping()


@mcp.tool()
def review_period(days: int = 7) -> dict:
    """retrospective for the last N days (default 7): what got done, what slipped,
    wins logged, completion rate over what was due, the busiest day, and the win
    gap. use for a real weekly review — reflect on how it went, name the pattern,
    then PROPOSE (never silently make) a plan for the next stretch."""
    return review_mod.review(days)


# ---- read -------------------------------------------------------------------

@mcp.tool()
def get_stats(start: str, end: str) -> dict:
    """aggregate totals over ANY date range (YYYY-MM-DD), no size cap: wins, active
    days, wins-by-month, todos completed, routine completions, appointments. use for
    long-term retrospection — 'how was 2024', all-time trends — where get_range is
    too narrow (it returns raw items, capped at 60 days; this returns counts)."""
    return review_mod.stats(start, end)


@mcp.tool()
def get_overview() -> dict:
    """snapshot of the user's life right now: today's items, upcoming appointments,
    recent achievements, and open todo count. use this to check in / coach."""
    today = date.today().isoformat()
    horizon = (date.today() + timedelta(days=14)).isoformat()
    todos = store.list_items("todos")
    appts = store.list_items("appointments")
    achs = store.list_items("achievements")
    return {
        "today": store.day(today),
        "upcoming_appointments": _upcoming(appts, today)[:10],
        "due_soon_todos": sorted(
            [t for t in todos if not t.get("done") and t.get("due")
             and today <= t["due"] <= horizon],
            key=lambda t: t["due"]),
        "open_todos": sum(1 for t in todos if not t.get("done")),
        "recent_achievements": sorted(
            achs, key=lambda a: (a.get("date", ""), a.get("created", "")),
            reverse=True)[:5],
        "total_achievements": len(achs),
    }


@mcp.tool()
def get_day(date_str: str) -> dict:
    """everything scheduled or achieved on one date (YYYY-MM-DD)."""
    return store.day(date_str)


@mcp.tool()
def list_achievements(limit: int = 25) -> list:
    """recent achievements, newest first."""
    achs = sorted(store.list_items("achievements"),
                  key=lambda a: (a.get("date", ""), a.get("created", "")), reverse=True)
    return achs[:max(1, limit)]


@mcp.tool()
def list_todos(include_done: bool = False) -> list:
    """todos. open only by default; pass include_done=true for the full list."""
    todos = store.list_items("todos")
    return todos if include_done else [t for t in todos if not t.get("done")]


@mcp.tool()
def list_appointments(upcoming_only: bool = True) -> list:
    """appointments, soonest first. upcoming_only resolves each to its next
    occurrence (recurring ones included) and hides fully-past ones."""
    today = date.today().isoformat()
    appts = store.list_items("appointments")
    if upcoming_only:
        return _upcoming(appts, today)
    return sorted(appts, key=lambda a: a.get("when", ""))


# ---- write ------------------------------------------------------------------

@mcp.tool()
def add_achievement(title: str, date: str = "", note: str = "") -> dict:
    """log a win. date defaults to today (YYYY-MM-DD). use this often to record progress."""
    return store.add_item("achievements", {"title": title, "date": date, "note": note})


@mcp.tool()
def add_todo(title: str, due: str = "", recur: str = "", interval: int = 1,
             until: str = "") -> dict:
    """add a todo. optional due date (YYYY-MM-DD) makes it a reminder + puts it on
    the calendar. to make it a repeating routine (e.g. 'workout', 'take meds'), set
    recur = 'daily' | 'weekly' | 'monthly' (+ interval, e.g. weekly interval=2 = every
    other week); due is the first/anchor date (defaults today). a routine shows up
    every occurrence and is checked off per-day, so it comes back the next day.
    until = 'YYYY-MM-DD' optionally caps the repeat."""
    item = {"title": title, "due": due}
    r = _recur(recur, interval, until)
    if r:
        item["recur"] = r
    return store.add_item("todos", item)


@mcp.tool()
def complete_todo(todo_id: str, date: str = "") -> dict:
    """mark a todo done by id. for a one-off this completes it; for a repeating
    routine this ticks a single day — date (YYYY-MM-DD) picks which, defaulting to
    today — so the routine reappears the next day."""
    item = store.set_todo_done(todo_id, date, True)
    return item or {"error": "todo not found", "id": todo_id}


@mcp.tool()
def add_appointment(title: str, when: str, end: str = "", location: str = "", note: str = "",
                    recur: str = "", interval: int = 1, until: str = "") -> dict:
    """add an appointment. when = 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'.
    end = the finish time, same format as when (e.g. when='2026-06-16 14:00',
    end='2026-06-16 15:00' = a 2–3pm block). all-day spans use dates. end is kept
    only when it falls after the start; leave it off for a point-in-time event.
    to repeat it, set recur = 'daily' | 'weekly' | 'monthly' and interval = N
    (e.g. recur='weekly', interval=2 on a thursday = every other thursday).
    until = 'YYYY-MM-DD' optionally caps the repeat (last possible occurrence)."""
    try:
        return store.add_item("appointments",
                              {"title": title, "when": when, "end": end, "location": location,
                               "note": note, "recur": _recur(recur, interval, until)})
    except store.SyncError:
        return {"error": "calendar server unreachable — not saved"}


@mcp.tool()
def update_appointment(item_id: str, title: str = "", when: str = "", end: str = "",
                       location: str = "", note: str = "",
                       recur: str = "", interval: int = 1, until: str = "") -> dict:
    """edit/reschedule an appointment by id. only non-empty fields are changed.
    when = 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'. end = finish time (same format) to
    make it a block; pass 'none' to clear the end. pass 'none' to clear location,
    note, or recur (recur='daily'|'weekly'|'monthly' + interval makes it repeat).
    until = 'YYYY-MM-DD' caps the repeat; 'none' clears the cap. changing recur
    without passing until keeps the existing end-date (never silently wipes it)."""
    patch = {}
    if title:
        patch["title"] = title
    if when:
        patch["when"] = when
    if end:
        patch["end"] = "" if end.lower() == "none" else end
    if location:
        patch["location"] = "" if location.lower() == "none" else location
    if note:
        patch["note"] = "" if note.lower() == "none" else note
    if recur:
        if recur.lower() == "none":
            patch["recur"] = ""
        else:
            u = (until or "").strip()
            keep = _current_recur(item_id).get("until", "")
            eff = "" if u.lower() == "none" else (u or keep)  # set / clear / preserve
            patch["recur"] = _recur(recur, interval, eff)
    elif until:
        # only the end-date is changing — keep the existing freq/interval.
        cur = _current_recur(item_id)
        if cur.get("freq"):
            u = until.strip()
            patch["recur"] = {**cur, "until": "" if u.lower() == "none" else u}
    try:
        return store.update_item("appointments", item_id, patch) or {"error": "not found", "id": item_id}
    except store.SyncError:
        return {"error": "calendar server unreachable — not saved"}


@mcp.tool()
def update_todo(item_id: str, title: str = "", due: str = "",
                recur: str = "", interval: int = 1, until: str = "") -> dict:
    """edit a todo by id: retitle, change/clear its due date, or turn repeating on/off.
    pass due='none' to clear it. recur = 'daily'|'weekly'|'monthly' (+ interval) makes
    it a routine; recur='none' makes it a one-off again. until caps the repeat."""
    patch = {}
    if title:
        patch["title"] = title
    if due:
        patch["due"] = "" if due.lower() == "none" else due
    if recur:
        patch["recur"] = "" if recur.lower() == "none" else _recur(recur, interval, until)
    return store.update_item("todos", item_id, patch) or {"error": "not found", "id": item_id}


@mcp.tool()
def update_achievement(item_id: str, title: str = "", date: str = "", note: str = "") -> dict:
    """edit a logged achievement by id. only non-empty fields are changed; pass
    note='none' to clear the note."""
    patch = {}
    if title:
        patch["title"] = title
    if date:
        patch["date"] = date
    if note:
        patch["note"] = "" if note.lower() == "none" else note
    return store.update_item("achievements", item_id, patch) or {"error": "not found", "id": item_id}


@mcp.tool()
def add_journal(text: str, when: str = "") -> dict:
    """write a diary/journal entry — a timestamped record of what happened, kept so
    it's still there to look back on years from now. `text` is the entry itself,
    free-form and any length (an event, a feeling, a milestone, how the day went).
    `when` defaults to right now; pass 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' to backdate
    a memory (e.g. logging something from last week). write one whenever the user
    recounts something worth remembering — don't wait to be asked to 'journal'."""
    return store.add_item("journal", {"body": text, "when": when})


@mcp.tool()
def list_journal(limit: int = 25) -> list:
    """recent diary entries, newest first (by their timestamp). use to recall what's
    been happening or to reflect a stretch of days back to the user."""
    js = sorted(store.list_items("journal"),
                key=lambda j: (j.get("when", ""), j.get("created", "")), reverse=True)
    return js[:max(1, limit)]


@mcp.tool()
def update_journal(item_id: str, text: str = "", when: str = "") -> dict:
    """edit a diary entry by id. only non-empty fields change; `when`
    ('YYYY-MM-DD' or with ' HH:MM') re-dates the entry."""
    patch = {}
    if text:
        patch["body"] = text
    if when:
        patch["when"] = when
    return store.update_item("journal", item_id, patch) or {"error": "not found", "id": item_id}


@mcp.tool()
def get_week() -> dict:
    """everything in the next 7 days, grouped by date — appointments, due todos, wins."""
    today = date.today()
    end = today + timedelta(days=6)
    return {"from": today.isoformat(),
            "days": store.days(today.isoformat(), end.isoformat())}


@mcp.tool()
def get_range(start: str, end: str) -> dict:
    """everything between two dates inclusive (YYYY-MM-DD), grouped by date —
    appointments, due todos, wins. use for retrospectives ('how did last week
    go?') or reviewing a past period. capped at 60 days."""
    try:
        s, e = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        return {"error": "dates must be YYYY-MM-DD"}
    if e < s:
        s, e = e, s
    if (e - s).days > 60:
        return {"error": "range too wide (max 60 days)"}
    return {"from": s.isoformat(), "to": e.isoformat(),
            "days": store.days(s.isoformat(), e.isoformat())}


@mcp.tool()
def delete_item(kind: str, item_id: str) -> dict:
    """delete an item. kind = achievements | todos | appointments | journal."""
    if kind not in store.ENTITIES:
        return {"error": f"kind must be one of {store.ENTITIES}"}
    try:
        return {"deleted": store.delete_item(kind, item_id), "id": item_id}
    except store.SyncError:
        return {"error": "calendar server unreachable — not deleted"}


if __name__ == "__main__":
    mcp.run()
