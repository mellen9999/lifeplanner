"""planning-partner read layer for lifeplanner.

pure, read-only derivations over the store: the facts a partner reasons from —
what's slipping right now, how a period went — so claude makes one call, not
eight. no writes, no llm, no heuristics: the meaning is claude's job, the facts
are ours. stdlib only; mirrors store's local-first, fail-safe style. every
function takes an optional `today` so the logic is deterministically testable.
"""

from datetime import date, timedelta

import store


def _today(today):
    return today or date.today()


def _open(todos):
    return [t for t in todos if not t.get("done")]


def _last_win(today):
    """(date, days-since) of the most recent logged win, or (None, None)."""
    dates = sorted((a.get("date", "") for a in store.list_items("achievements")
                    if a.get("date")), reverse=True)
    if not dates:
        return None, None
    try:
        return dates[0], (today - date.fromisoformat(dates[0])).days
    except ValueError:
        return dates[0], None


def whats_slipping(today=None, stale_after=14, horizon=2):
    """present-tense attention list — what a partner notices first: overdue open
    todos (most late first), stale undated todos (open longer than `stale_after`
    days), the win gap, and the next `horizon` days' load. all derived, no
    judgement — claude reads the gaps and decides what's urgent."""
    today = _today(today)
    iso = today.isoformat()
    todos = store.list_items("todos")
    # routines (recurring todos) are never "overdue" or "stale" — a missed day isn't
    # a deadline slipping, it's just a day. they surface as today's checklist instead.
    one_off = _open([t for t in todos if not t.get("recur")])

    overdue = []
    for t in one_off:
        due = t.get("due")
        if due and due < iso:
            try:
                overdue.append({**t, "days_late": (today - date.fromisoformat(due)).days})
            except ValueError:
                pass
    overdue.sort(key=lambda t: t["days_late"], reverse=True)

    stale = []
    cutoff = (today - timedelta(days=stale_after)).isoformat()
    for t in one_off:
        if t.get("due"):
            continue
        created = (t.get("created") or "")[:10]
        if created and created < cutoff:
            try:
                stale.append({**t, "age_days": (today - date.fromisoformat(created)).days})
            except ValueError:
                pass
    stale.sort(key=lambda t: t["age_days"], reverse=True)

    # today's routines still to do — recurring todos due today, not yet ticked
    routines = [{"id": t.get("id"), "title": t.get("title", "")}
                for t in todos if t.get("recur")
                and store.todo_occurrences(t, iso, iso)
                and not store.todo_done_on(t, iso)]

    last_win, days_since = _last_win(today)

    end = (today + timedelta(days=max(1, horizon) - 1)).isoformat()
    load = store.days(iso, end)
    upcoming = []
    for d in sorted(load):
        for a in load[d]["appointments"]:
            upcoming.append({"date": d, "kind": "appointment",
                             "title": a.get("title", ""), "when": a.get("when", "")})
        for t in load[d]["todos"]:
            upcoming.append({"date": d, "kind": "todo", "title": t.get("title", "")})

    return {
        "today": iso,
        "overdue_todos": overdue,
        "stale_todos": stale,
        "routines_today": routines,
        "last_win_date": last_win,
        "days_since_win": days_since,
        "next_load": upcoming,
    }


def _busiest(grid):
    best, best_n = None, 0
    for d, slot in grid.items():
        n = len(slot["appointments"]) + len(slot["todos"]) + len(slot["achievements"])
        if n > best_n:
            best, best_n = d, n
    return {"date": best, "items": best_n} if best else None


def stats(start, end):
    """aggregate totals over [start, end] inclusive — wins, active days, wins by
    month, todos completed, routine completions, appointments. counts only (no raw
    items), so it spans any range cheaply — built for long-term retrospection
    ('how was 2024', all-time) where get_range's 60-day cap is too narrow."""
    try:
        s, e = date.fromisoformat(start[:10]), date.fromisoformat(end[:10])
    except (TypeError, ValueError):
        return {"error": "dates must be YYYY-MM-DD"}
    if e < s:
        s, e = e, s
    si, ei = s.isoformat(), e.isoformat()
    wins = [a for a in store.list_items("achievements")
            if a.get("date") and si <= a["date"] <= ei]
    by_month = {}
    for a in wins:
        by_month[a["date"][:7]] = by_month.get(a["date"][:7], 0) + 1
    todos = store.list_items("todos")
    todos_done = sum(1 for t in todos if not t.get("recur") and t.get("done")
                     and si <= (t.get("done_at") or "") <= ei)
    routine_done = sum(1 for t in todos if t.get("recur")
                       for x in (t.get("done_dates") or []) if si <= x <= ei)
    appts = sum(len(store.occurrences_in(a, si, ei))
                for a in store.list_items("appointments"))
    return {
        "from": si, "to": ei,
        "wins": len(wins),
        "active_days": len({a["date"] for a in wins}),
        "wins_by_month": dict(sorted(by_month.items())),
        "todos_completed": todos_done,
        "routine_completions": routine_done,
        "appointments": appts,
    }


def review(days=7, today=None):
    """retrospective digest for the last `days` days (inclusive of today): what got
    done, what slipped, the wins, completion rate over what was due, busiest day,
    and the win gap — one call for 'how did the week go, let's plan the next'."""
    try:
        days = max(1, min(int(days), 60))
    except (TypeError, ValueError):
        days = 7
    today = _today(today)
    start = today - timedelta(days=days - 1)
    s, e = start.isoformat(), today.isoformat()

    todos = store.list_items("todos")
    # the completion rate is about *deadlines* — one-off dated todos only. routines
    # (recurring) aren't deadlines, so they don't drag the rate up or down.
    due_in = [t for t in todos if t.get("due") and not t.get("recur") and s <= t["due"] <= e]
    done_due = [t for t in due_in if t.get("done")]
    slipped = [t for t in due_in if not t.get("done")]
    # broader momentum: everything finished in-window, including undated todos.
    completed = [t for t in todos if not t.get("recur") and t.get("done")
                 and s <= (t.get("done_at") or "") <= e]
    # routine consistency: per routine, how many of its occurrences in the window
    # got ticked (e.g. workout 4/7). this is the habit-tracker payoff — it shows
    # which routines are holding and which are slipping.
    routines = []
    for t in todos:
        if not t.get("recur"):
            continue
        total = len(store.todo_occurrences(t, s, e))
        if not total:
            continue
        done = sum(1 for x in (t.get("done_dates") or []) if s <= x <= e)
        routines.append({"title": t.get("title", ""), "done": done, "total": total})
    routine_done = sum(r["done"] for r in routines)
    routine_total = sum(r["total"] for r in routines)

    wins = [a for a in store.list_items("achievements")
            if a.get("date") and s <= a["date"] <= e]

    grid = store.days(s, e)
    occurred = [{"date": d, "title": a.get("title", ""), "when": a.get("when", "")}
                for d in sorted(grid) for a in grid[d]["appointments"]]

    _, win_gap = _last_win(today)
    return {
        "from": s, "to": e, "days": days,
        "completed_todos": completed,
        "wins": wins,
        "appointments_occurred": occurred,
        "slipped_todos": slipped,
        "due_in_window": len(due_in),
        "completed_due": len(done_due),
        "completion_rate": round(len(done_due) / len(due_in), 2) if due_in else None,
        "wins_count": len(wins),
        "routine_completions": routine_done,
        "routine_total": routine_total,
        "routines": sorted(routines, key=lambda r: r["done"] / r["total"]),
        "busiest_day": _busiest(grid),
        "days_since_win": win_gap,
    }
