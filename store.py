"""shared data layer for lifeplanner.

single source of truth touched by both app.pyw (web ui) and mcp_server.py (claude).
local json files, atomic writes, cross-process lockfile, .ics generation. stdlib only.
"""

import io
import json
import os
import stat
import time
import zipfile
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

__version__ = "1.0.0"

BASE = Path(__file__).resolve().parent
# data dir is configurable so the app is portable (clone-and-run, or point at a
# synced/XDG location). everything generated lives here and is gitignored.
DATA = Path(os.environ.get("LIFEPLANNER_DATA") or (BASE / "data")).expanduser()
LOCK = DATA / ".lock"
ICS = DATA / "lifeplanner.ics"
# appointments cache — only used in caldav mode, so the desktop still shows the
# last-known appointments when the caldav server is briefly unreachable.
APPT_CACHE = DATA / "appointments.cache.json"

# optional caldav backend for appointments. when .caldav.json is present, the
# appointments entity is backed by a shared caldav server (radicale, two-way with
# the phone); otherwise everything stays local json. import is soft so the app
# still runs if the optional deps (icalendar/defusedxml) aren't installed.
if os.environ.get("LIFEPLANNER_CALDAV", "").lower() in ("0", "off", "false", "no"):
    caldav_store = None          # explicitly disabled (tests, local-only mode)
    _CALDAV = None
else:
    try:
        import caldav_store
        _CALDAV = caldav_store.config()
    except Exception:
        caldav_store = None
        _CALDAV = None


def caldav_enabled():
    return _CALDAV is not None


class SyncError(Exception):
    """appointment sync backend (caldav server) was unreachable for a write —
    callers surface this to the user instead of crashing or losing the change."""


# whether the last appointments read came live from the server or fell back to
# cache — surfaced to the ui so it never silently shows stale data as current.
_appt_source = "local"


def appointments_status():
    if _CALDAV is None:
        return {"backend": "local", "source": "local"}
    return {"backend": "caldav", "source": _appt_source}

ENTITIES = ("achievements", "todos", "appointments")
# fields a PATCH is allowed to touch, per entity — anything else is dropped so a
# stray ui/llm key can't pollute stored items. id/created are never patchable.
PATCHABLE = {
    "achievements": ("title", "date", "note"),
    "todos": ("title", "done", "due", "recur", "order"),
    "appointments": ("title", "when", "location", "note", "recur"),
}
RECUR_FREQS = ("daily", "weekly", "monthly")
DEFAULT_SETTINGS = {"theme": "dark", "accent": "#ff8700", "ics_sync_path": ""}


def _ensure():
    DATA.mkdir(parents=True, exist_ok=True)
    # data holds sensitive titles (health/legal appointments) — keep it private.
    try:
        DATA.chmod(0o700)
    except OSError:
        pass


# ---- cross-process lock -----------------------------------------------------

class FileLock:
    """exclusive lock via O_EXCL create. stale locks (>10s) are reclaimed.

    serializes the rare case where the ui and claude write the same instant.
    reads never lock.
    """

    def __init__(self, timeout=5.0):
        self.timeout = timeout
        self.fd = None

    def __enter__(self):
        _ensure()
        start = time.monotonic()
        while True:
            try:
                self.fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            # PermissionError can surface instead of FileExistsError on windows
            # (av/indexer holding the handle); treat both as "held".
            except (FileExistsError, PermissionError):
                # reclaim a stale lock, but never follow a symlink to unlink it
                try:
                    st = os.lstat(LOCK)
                    if not stat.S_ISLNK(st.st_mode) and time.time() - st.st_mtime > 10:
                        os.unlink(LOCK)
                except FileNotFoundError:
                    pass
                if time.monotonic() - start > self.timeout:
                    raise TimeoutError("lifeplanner data lock is busy")
                time.sleep(0.02)

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
            try:
                os.unlink(LOCK)
            except FileNotFoundError:
                pass


# ---- raw json io ------------------------------------------------------------

def _path(name):
    return DATA / f"{name}.json"


def _read_raw(name, fallback):
    p = _path(name)
    if not p.exists():
        return fallback
    try:
        return json.loads(p.read_text("utf-8"))
    except json.JSONDecodeError:
        # corrupt/unparseable file — fail safe to the default. an OSError (perms,
        # FS error) is deliberately NOT caught: swallowing it would let a
        # read-modify-write (e.g. add_item) overwrite real data with an empty
        # list. surface it loudly so a transient fault can't masquerade as "no data".
        return fallback


def _write_raw(name, value):
    _ensure()
    p = _path(name)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), "utf-8")
    try:
        tmp.chmod(0o600)  # private — these files carry health/legal titles
    except OSError:
        pass
    os.replace(tmp, p)  # atomic on the same filesystem


# ---- entities ---------------------------------------------------------------

# ---- caldav-backed appointments ---------------------------------------------

def _cache_write(items):
    try:
        _write_raw("appointments.cache", items)
    except OSError:
        pass


def _cache_read():
    try:
        items = _read_raw("appointments.cache", [])
    except OSError:
        return []  # cache is best-effort; a read fault just means "no cache"
    return items if isinstance(items, list) else []


def _caldav_list():
    """live appointments from the server; on any failure fall back to the last
    cached copy so the ui never blanks. refreshes the cache on success and records
    whether the data is live or stale (surfaced to the ui)."""
    global _appt_source
    try:
        items = caldav_store.list_appointments(_CALDAV)
    except caldav_store.CalDAVError:
        _appt_source = "cache"
        return _cache_read()
    items.sort(key=lambda a: a.get("when", ""))
    _cache_write(items)
    _appt_source = "live"
    return items


def _caldav_refresh():
    """re-pull after a mutation so cache + .ics reflect server truth."""
    try:
        _caldav_list()
        regen_ics()
    except (caldav_store.CalDAVError, OSError):
        pass


def list_items(name):
    if name not in ENTITIES:
        raise ValueError(f"unknown entity: {name}")
    if name == "appointments" and _CALDAV is not None:
        return _caldav_list()
    items = _read_raw(name, [])
    if not isinstance(items, list):
        return []
    # drop any non-dict element so a poisoned/partially-written file can't crash
    # every caller that does item.get(...) downstream (state, day, occurrences).
    return [it for it in items if isinstance(it, dict)]


def _coerce(name, key, value):
    """normalize one field to its stored form. the single place field validation
    lives — so add and every update path coerce identically and can never drift
    (a bad date/rule can't slip in through one door and crash expansion later)."""
    if key == "recur":
        return _norm_recur(value)
    if key == "when":
        return _norm_when(str(value or "").strip())
    if key == "due":
        s = str(value or "").strip()
        return _norm_date(s) if s else ""
    if key == "date":
        return _norm_date(str(value or "").strip())
    if key in ("title", "location", "note"):
        return str(value or "").strip()
    if key == "done":
        return bool(value)
    if key == "order":  # manual sort position (0 = unset → sorts to the end)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return value


def _normalize(name, item):
    """coerce + default every field so the stored item is well-formed. loud on no title."""
    title = _coerce(name, "title", item.get("title"))
    if not title:
        raise ValueError("title is required")
    base = {"id": uuid4().hex[:12], "title": title,
            "created": datetime.now().isoformat(timespec="seconds")}
    for key in PATCHABLE[name]:
        if key != "title":
            base[key] = _coerce(name, key, item.get(key))
    # done_at is server-stamped (not client-patchable), but keep the key present
    # so the stored shape is uniform — and carry it through an undo-restore.
    # done_dates tracks per-day completion of a *recurring* todo (a routine like
    # "workout" is done on specific dates, never globally) — validated, deduped.
    if name == "todos":
        base["done_at"] = str(item.get("done_at") or "")
        base["done_dates"] = _valid_dates(item.get("done_dates"))
        # a routine needs a `due` anchor to recur from — without one it would never
        # produce an occurrence. default it to today so "repeat" always works.
        if base.get("recur") and not base.get("due"):
            base["due"] = date.today().isoformat()
    return base


def add_item(name, item):
    new = _normalize(name, item)
    if name == "appointments" and _CALDAV is not None:
        try:
            caldav_store.put_appointment(_CALDAV, new)
        except caldav_store.CalDAVError as e:
            raise SyncError(str(e)) from e
        _caldav_refresh()
        return new
    with FileLock():
        items = list_items(name)
        items.append(new)
        _write_raw(name, items)
        _regen_ics_locked()
    return new


def update_item(name, item_id, patch):
    if name == "appointments" and _CALDAV is not None:
        return _caldav_update(item_id, patch)
    with FileLock():
        items = list_items(name)
        found = None
        for it in items:
            if it.get("id") == item_id:
                allowed = PATCHABLE.get(name, ())
                for k, v in patch.items():
                    if k in allowed:
                        it[k] = _coerce(name, k, v)
                if name == "todos" and it.get("recur"):
                    # a recurring todo's completion is per-date (done_dates), set via
                    # set_todo_done — the global done flag never applies to a routine,
                    # so retire it here (covers a one-off edited into a routine too).
                    it["done"], it["done_at"] = False, ""
                elif name == "todos" and "done" in patch:
                    # stamp when a one-off todo was completed (cleared if reopened) so
                    # the ui can show it and "what did i finish" is answerable.
                    it["done_at"] = date.today().isoformat() if it.get("done") else ""
                found = it
                break
        if found is None:
            return None
        _write_raw(name, items)
        _regen_ics_locked()
    return found


def set_todo_done(item_id, on_date, done):
    """mark a todo complete/incomplete. a recurring todo (routine) records the date
    in done_dates so each day stands alone; a one-off uses the global done flag.
    on_date defaults to today. returns the updated item, or None if not found."""
    day = _norm_date(on_date) if on_date else date.today().isoformat()
    with FileLock():
        items = list_items("todos")
        t = next((x for x in items if x.get("id") == item_id), None)
        if t is None:
            return None
        if t.get("recur"):
            dd = set(_valid_dates(t.get("done_dates")))
            dd.add(day) if done else dd.discard(day)
            t["done_dates"] = sorted(dd)
        else:
            t["done"] = bool(done)
            t["done_at"] = date.today().isoformat() if done else ""
        _write_raw("todos", items)
        _regen_ics_locked()
    return t


def reorder_todos(ids):
    """set each todo's manual order to its position in `ids` (1-based; others left
    alone). one atomic write for a whole drag-reorder. order 0 = unset → sorts last."""
    if not isinstance(ids, list):
        return False
    pos = {i: n + 1 for n, i in enumerate(ids) if isinstance(i, str)}
    with FileLock():
        items = list_items("todos")
        for t in items:
            if t.get("id") in pos:
                t["order"] = pos[t["id"]]
        _write_raw("todos", items)
    return True


def _caldav_update(item_id, patch):
    """patch an appointment in place on the server. only fields that actually
    changed are written, so editing (say) the title of a phone-made event never
    rewrites — and so never destroys — its timezone or recurrence."""
    cur = next((a for a in _caldav_list() if a.get("id") == item_id), None)
    if cur is None:
        return None
    allowed = PATCHABLE.get("appointments", ())
    changed = set()
    for k, v in patch.items():
        if k not in allowed:
            continue
        nv = _coerce("appointments", k, v)
        if nv != cur.get(k):
            cur[k] = nv
            changed.add(k)
    if changed:
        try:
            caldav_store.put_appointment(_CALDAV, cur, changed=changed)
        except caldav_store.CalDAVError as e:
            raise SyncError(str(e)) from e
        _caldav_refresh()
    return cur


def delete_item(name, item_id):
    if name == "appointments" and _CALDAV is not None:
        cur = next((a for a in _caldav_list() if a.get("id") == item_id), None)
        if cur is None:
            return False
        try:
            ok = caldav_store.delete_appointment(_CALDAV, item_id, cur.get("_href"))
        except caldav_store.CalDAVError as e:
            raise SyncError(str(e)) from e
        _caldav_refresh()
        return ok
    with FileLock():
        items = list_items(name)
        kept = [it for it in items if it.get("id") != item_id]
        if len(kept) == len(items):
            return False
        _write_raw(name, kept)
        _regen_ics_locked()
    return True


# ---- settings ---------------------------------------------------------------

def get_settings():
    s = _read_raw("settings", {})
    if not isinstance(s, dict):
        s = {}
    return {**DEFAULT_SETTINGS, **s}


def put_settings(patch):
    with FileLock():
        s = get_settings()
        s.update({k: patch[k] for k in DEFAULT_SETTINGS if k in patch})
        _write_raw("settings", s)
    return s


# ---- date helpers -----------------------------------------------------------

def _norm_date(s):
    """accept YYYY-MM-DD (or anything date.fromisoformat eats); fall back to today."""
    try:
        return date.fromisoformat(s[:10]).isoformat()
    except (ValueError, TypeError):
        return date.today().isoformat()


def _valid_dates(seq):
    """sorted, deduped set of valid YYYY-MM-DD from a loose list — anything
    unparseable is dropped (never coerced to today, which would forge completions)."""
    out = set()
    for x in (seq or []):
        if isinstance(x, str):
            try:
                out.add(date.fromisoformat(x[:10]).isoformat())
            except ValueError:
                pass
    return sorted(out)


def _norm_when(s):
    """accept 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' / 'YYYY-MM-DDTHH:MM'. keep time if present."""
    if not s:
        return date.today().isoformat()
    s = s.replace("T", " ").strip()
    try:
        if len(s) <= 10:
            return date.fromisoformat(s[:10]).isoformat()
        dt = datetime.strptime(s[:16], "%Y-%m-%d %H:%M")
        return dt.isoformat(timespec="minutes")
    except ValueError:
        return _norm_date(s)


def when_date(when):
    """the YYYY-MM-DD a thing falls on, ignoring time."""
    return (when or "")[:10]


# ---- recurrence -------------------------------------------------------------
# an appointment may repeat. recur is "" (one-time) or a small validated dict
# {freq: daily|weekly|monthly, interval: >=1, until: "YYYY-MM-DD"|""}. weekly
# naturally keeps the anchor's weekday, so "every other thursday" is just a
# thursday anchor with freq=weekly, interval=2.

def _norm_recur(r):
    """coerce any input into a valid recur dict, or "" if not a real rule."""
    if not r:
        return ""
    if isinstance(r, str):
        r = {"freq": r}
    if not isinstance(r, dict):
        return ""
    freq = str(r.get("freq", "")).strip().lower()
    if freq not in RECUR_FREQS:
        return ""
    try:
        interval = max(1, int(r.get("interval", 1) or 1))
    except (TypeError, ValueError):
        interval = 1
    until = ""
    u = str(r.get("until") or "").strip()
    if u:
        try:
            until = date.fromisoformat(u[:10]).isoformat()
        except ValueError:
            until = ""
    return {"freq": freq, "interval": interval, "until": until}


def _occurrences(when, recur, start_iso, end_iso):
    """when-strings for every time the (anchor `when`, `recur`) series falls within
    [start, end] inclusive. non-recurring → its single date if in range. preserves
    any time component. monthly follows RRULE semantics: anchors on the start day
    and skips months that lack it (jan 31 → mar 31, no feb) so the app matches the
    phone .ics. shared by appointments (anchor=when) and todos (anchor=due)."""
    if not when:
        return []
    try:
        anchor = date.fromisoformat(when[:10])
        start = date.fromisoformat(start_iso[:10])
        end = date.fromisoformat(end_iso[:10])
    except ValueError:
        return []
    time_part = when[10:]  # "" for all-day, else "THH:MM"
    if not recur:
        return [when] if start <= anchor <= end else []
    freq, interval = recur["freq"], max(1, recur.get("interval", 1))
    until = date.fromisoformat(recur["until"]) if recur.get("until") else None
    limit = end if until is None else min(end, until)
    out = []
    if freq == "monthly":
        for k in range(10000):
            tot = anchor.month - 1 + interval * k
            y, m = anchor.year + tot // 12, tot % 12 + 1
            if date(y, m, 1) > limit:
                break
            if anchor.day > monthrange(y, m)[1]:
                continue  # this month has no such day — skip it
            d = date(y, m, anchor.day)
            if start <= d <= limit:
                out.append(d.isoformat() + time_part)
    else:
        step = timedelta(days=interval if freq == "daily" else 7 * interval)
        d, guard = anchor, 0
        while d <= limit and guard < 100000:
            guard += 1
            if d >= start:
                out.append(d.isoformat() + time_part)
            d += step
    return out


def occurrences_in(appt, start_iso, end_iso):
    """appointment occurrences in [start, end] — anchored on its `when`."""
    return _occurrences(appt.get("when", ""), appt.get("recur") or "", start_iso, end_iso)


def todo_occurrences(todo, start_iso, end_iso):
    """todo due-dates in [start, end] — anchored on its `due`. a recurring todo
    (a routine) expands to every occurrence; a one-off resolves to its single due."""
    return _occurrences(todo.get("due", ""), todo.get("recur") or "", start_iso, end_iso)


def todo_done_on(todo, day_iso):
    """is this todo complete for the given date? recurring → that date is in
    done_dates; one-off → the global done flag (date ignored)."""
    if todo.get("recur"):
        return day_iso in (todo.get("done_dates") or [])
    return bool(todo.get("done"))


def next_occurrence(appt, on_or_after_iso):
    """the soonest when-string on/after the given date, or None."""
    horizon = (date.fromisoformat(on_or_after_iso[:10]) + timedelta(days=366 * 5)).isoformat()
    occ = occurrences_in(appt, on_or_after_iso, horizon)
    return occ[0] if occ else None


# ---- aggregate views --------------------------------------------------------

def state():
    """everything the ui needs in one shot."""
    return {
        "achievements": sorted(list_items("achievements"),
                               key=lambda a: (a.get("date", ""), a.get("created", "")),
                               reverse=True),
        "todos": list_items("todos"),
        "appointments": sorted(list_items("appointments"),
                               key=lambda a: a.get("when", "")),
        "sync": appointments_status(),
        "settings": get_settings(),
        "version": version(),
    }


def version():
    """cheap change token for the ui poller. nanosecond mtimes so two quick writes
    never collapse; in caldav mode it also folds in the server's collection tag so
    a change made on the phone flips the token and the desktop refreshes live."""
    # derived from ENTITIES (+ settings) so a new entity's writes always flip the
    # token; in caldav mode appointments live on the server, so watch the cache.
    names = ["settings"]
    for e in ENTITIES:
        names.append("appointments.cache" if (e == "appointments" and _CALDAV is not None) else e)
    latest = 0
    for name in names:
        try:
            latest = max(latest, _path(name).stat().st_mtime_ns)
        except OSError:
            pass
    token = str(latest)
    if _CALDAV is not None:
        ctag = caldav_store.collection_ctag(_CALDAV, timeout=4)
        token += "|" + (ctag or "offline")
    return token


def days(start, end):
    """every non-empty day in [start, end] (inclusive), keyed by YYYY-MM-DD, each
    {date, appointments, todos, achievements}. reads each list ONCE and buckets in
    memory — so a multi-day view costs 3 reads, not 3 per day. recurring
    appointments are expanded to every occurrence in range (shown, not the anchor)."""
    s, e = _norm_date(start), _norm_date(end)
    out = {}

    def slot(d):
        return out.setdefault(
            d, {"date": d, "appointments": [], "todos": [], "achievements": []})

    for a in list_items("appointments"):
        for w in occurrences_in(a, s, e):
            slot(w[:10])["appointments"].append({**a, "when": w})
    # recurring todos (routines) expand to every occurrence in range, each tagged
    # with its occurrence date (`due`) + whether it's done on that day, so the ui
    # can render and tick the right instance. one-off todos drop on their due.
    for t in list_items("todos"):
        if t.get("recur"):
            for d in todo_occurrences(t, s, e):
                slot(d[:10])["todos"].append({**t, "due": d[:10], "done": d[:10] in (t.get("done_dates") or [])})
        else:
            due = t.get("due")
            if due and s <= due <= e:
                slot(due)["todos"].append(t)
    for a in list_items("achievements"):
        dt = a.get("date")
        if dt and s <= dt <= e:
            slot(dt)["achievements"].append(a)
    return out


def day(target):
    """all items on a given YYYY-MM-DD date (the empty shape if nothing falls on it)."""
    d = _norm_date(target)
    return days(d, d).get(d, {"date": d, "appointments": [], "todos": [], "achievements": []})


# ---- .ics generation --------------------------------------------------------

def _ics_escape(text):
    # strip CR first so a bare \r can't forge a line break and inject a property
    return (str(text).replace("\\", "\\\\").replace("\r", "")
            .replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n"))


def _fold(line):
    """RFC5545: lines >75 octets are folded with CRLF + leading space."""
    out = []
    while len(line.encode("utf-8")) > 75:
        # step back to a safe utf-8 boundary under 75 bytes
        cut = 75
        while len(line[:cut].encode("utf-8")) > 75:
            cut -= 1
        out.append(line[:cut])
        line = " " + line[cut:]
    out.append(line)
    return "\r\n".join(out)


def _rrule(recur, all_day):
    """RFC5545 RRULE line for a recur dict, or "" — phones expand it natively."""
    if not recur:
        return ""
    freq = {"daily": "DAILY", "weekly": "WEEKLY", "monthly": "MONTHLY"}.get(recur.get("freq"))
    if not freq:
        return ""
    parts = [f"FREQ={freq}"]
    if recur.get("interval", 1) > 1:
        parts.append(f"INTERVAL={recur['interval']}")
    until = recur.get("until") or ""
    if until:
        u = until.replace("-", "")
        parts.append(f"UNTIL={u}" if all_day else f"UNTIL={u}T235959")
    return "RRULE:" + ";".join(parts)


def _vevent(uid, summary, dtstart, all_day, desc, location, recur=""):
    # DTSTAMP is REQUIRED by RFC5545 §3.6.1 — strict importers (radicale,
    # thunderbird) reject events without it.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VEVENT", f"UID:{uid}@lifeplanner", f"DTSTAMP:{stamp}"]
    if all_day:
        lines.append(f"DTSTART;VALUE=DATE:{dtstart:%Y%m%d}")
    else:
        # floating local time (no TZID) — matches "when" with no zone info
        lines.append(f"DTSTART:{dtstart:%Y%m%dT%H%M%S}")
    rrule = _rrule(recur, all_day)
    if rrule:
        lines.append(rrule)
    lines.append(f"SUMMARY:{_ics_escape(summary)}")
    if desc:
        lines.append(f"DESCRIPTION:{_ics_escape(desc)}")
    if location:
        lines.append(f"LOCATION:{_ics_escape(location)}")
    lines.append("END:VEVENT")
    return [_fold(x) for x in lines]


def build_ics():
    """appointments + due-dated todos as a read-only VCALENDAR string."""
    out = ["BEGIN:VCALENDAR", "VERSION:2.0",
           "PRODID:-//lifeplanner//EN", "CALSCALE:GREGORIAN",
           "X-WR-CALNAME:lifeplanner"]
    for ap in list_items("appointments"):
        when = ap.get("when", "")
        all_day = len(when) <= 10
        try:
            dt = date.fromisoformat(when[:10]) if all_day else datetime.fromisoformat(when)
        except ValueError:
            continue
        out += _vevent(ap.get("id", ""), ap.get("title", "appointment"), dt,
                       all_day, ap.get("note", ""), ap.get("location", ""),
                       ap.get("recur", ""))
    for td in list_items("todos"):
        due = td.get("due", "")
        recur = td.get("recur") or ""
        # one-off: skip if undated or done. recurring (routine): always emit as a
        # repeating all-day event so the phone calendar shows it every day (per-day
        # completion isn't expressible in a feed, so the series is shown in full).
        if not due or (not recur and td.get("done")):
            continue
        try:
            dt = date.fromisoformat(due[:10])
        except ValueError:
            continue
        out += _vevent(td.get("id", ""), "todo: " + td.get("title", ""), dt, True, "", "", recur)
    out.append("END:VCALENDAR")
    return "\r\n".join(out) + "\r\n"


def _write_ics(dst, blob):
    # write bytes (not text) so windows text-mode doesn't turn the RFC5545 \r\n
    # line endings into \r\r\n. atomic via temp + replace.
    tmp = dst.with_suffix(".tmp")
    tmp.write_bytes(blob)
    try:
        tmp.chmod(0o600)  # appointment titles are private (health/legal)
    except OSError:
        pass
    os.replace(tmp, dst)


def _regen_ics_locked():
    """rewrite the .ics feed (+ optional sync copy). caller already holds the lock."""
    blob = build_ics().encode("utf-8")
    try:
        _write_ics(ICS, blob)
    except OSError:
        pass
    sync = get_settings().get("ics_sync_path", "").strip()
    if sync:
        try:
            dst = Path(sync).expanduser()
            if dst.is_dir():
                dst = dst / "lifeplanner.ics"
            # only ever write a .ics file — never let the sync path overwrite an
            # arbitrary file (e.g. a dotfile) if a ui/llm sets a bad value.
            if dst.suffix.lower() == ".ics":
                _write_ics(dst, blob)
        except OSError:
            pass  # never let a bad sync path break a write


def regen_ics():
    with FileLock():
        _regen_ics_locked()


# ---- export -----------------------------------------------------------------

def export_bytes():
    """all user data as a zip of the source-of-truth json — one-click backup /
    portability. read under the lock so it's a consistent multi-file snapshot;
    restore by unzipping back into the data dir."""
    # derived from ENTITIES so a new entity can never be silently left out of a
    # backup. settings + the caldav cache round out the on-disk vault.
    names = (*ENTITIES, "appointments.cache", "settings")
    # read raw bytes under the lock (a consistent snapshot), then compress
    # outside it — compression is CPU-bound and must not block concurrent writes.
    blobs = {}
    with FileLock():
        for name in names:
            p = _path(name)
            if p.exists():
                blobs[f"{name}.json"] = p.read_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for fn, data in blobs.items():
            z.writestr(fn, data)
    return buf.getvalue()
