#!/usr/bin/env python3
"""lifeplanner MCP server — claude's door into the same local store.

reads fresh on every call (no cache), so mellen's ui edits are always visible.
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


def _recur(recur, interval):
    """build a recur rule from loose tool args, or '' for one-time."""
    return {"freq": recur, "interval": interval} if recur else ""


# ---- read -------------------------------------------------------------------

@mcp.tool()
def get_overview() -> dict:
    """snapshot of mellen's life right now: today's items, upcoming appointments,
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
def add_todo(title: str, due: str = "") -> dict:
    """add a todo. optional due date (YYYY-MM-DD) makes it a reminder + puts it on the calendar."""
    return store.add_item("todos", {"title": title, "due": due})


@mcp.tool()
def complete_todo(todo_id: str) -> dict:
    """mark a todo done by id."""
    item = store.update_item("todos", todo_id, {"done": True})
    return item or {"error": "todo not found", "id": todo_id}


@mcp.tool()
def add_appointment(title: str, when: str, location: str = "", note: str = "",
                    recur: str = "", interval: int = 1) -> dict:
    """add an appointment. when = 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'.
    to repeat it, set recur = 'daily' | 'weekly' | 'monthly' and interval = N
    (e.g. recur='weekly', interval=2 on a thursday = every other thursday)."""
    return store.add_item("appointments",
                          {"title": title, "when": when, "location": location,
                           "note": note, "recur": _recur(recur, interval)})


@mcp.tool()
def update_appointment(item_id: str, title: str = "", when: str = "",
                       location: str = "", note: str = "",
                       recur: str = "", interval: int = 1) -> dict:
    """edit/reschedule an appointment by id. only non-empty fields are changed.
    when = 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'. set recur='daily'|'weekly'|'monthly'
    (+ interval) to make it repeat, or recur='none' to clear repetition."""
    patch = {k: v for k, v in
             {"title": title, "when": when, "location": location, "note": note}.items() if v}
    if recur:
        patch["recur"] = "" if recur.lower() == "none" else _recur(recur, interval)
    return store.update_item("appointments", item_id, patch) or {"error": "not found", "id": item_id}


@mcp.tool()
def update_todo(item_id: str, title: str = "", due: str = "") -> dict:
    """edit a todo by id (retitle or change/clear its due date). pass due='none' to clear it."""
    patch = {}
    if title:
        patch["title"] = title
    if due:
        patch["due"] = "" if due.lower() == "none" else due
    return store.update_item("todos", item_id, patch) or {"error": "not found", "id": item_id}


@mcp.tool()
def update_achievement(item_id: str, title: str = "", date: str = "", note: str = "") -> dict:
    """edit a logged achievement by id. only non-empty fields are changed."""
    patch = {k: v for k, v in {"title": title, "date": date, "note": note}.items() if v}
    return store.update_item("achievements", item_id, patch) or {"error": "not found", "id": item_id}


@mcp.tool()
def get_week() -> dict:
    """everything in the next 7 days, grouped by date — appointments, due todos, wins."""
    today = date.today()
    out = {}
    for i in range(7):
        d = (today + timedelta(days=i)).isoformat()
        day = store.day(d)
        if day["appointments"] or day["todos"] or day["achievements"]:
            out[d] = day
    return {"from": today.isoformat(), "days": out}


@mcp.tool()
def delete_item(kind: str, item_id: str) -> dict:
    """delete an item. kind = achievements | todos | appointments."""
    if kind not in store.ENTITIES:
        return {"error": f"kind must be one of {store.ENTITIES}"}
    return {"deleted": store.delete_item(kind, item_id), "id": item_id}


if __name__ == "__main__":
    mcp.run()
