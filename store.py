"""shared data layer for lifeplanner.

single source of truth touched by both app.pyw (web ui) and mcp_server.py (claude).
local json files, atomic writes, cross-process lockfile, .ics generation. stdlib only.
"""

import json
import os
import stat
import time
from calendar import monthrange
from datetime import date, datetime, timedelta
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
# last-known appointments when the server (mele) is briefly unreachable.
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
    "todos": ("title", "done", "due"),
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
    except (json.JSONDecodeError, OSError):
        # never crash on a corrupt/locked file — fail safe to the default
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
    items = _read_raw("appointments.cache", [])
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
    return items if isinstance(items, list) else []


def _normalize(name, item):
    """coerce + default fields so every item is well-formed. fails loud on no title."""
    title = str(item.get("title", "")).strip()
    if not title:
        raise ValueError("title is required")
    now = datetime.now().isoformat(timespec="seconds")
    base = {"id": uuid4().hex[:12], "title": title, "created": now}
    if name == "achievements":
        d = str(item.get("date") or "").strip() or date.today().isoformat()
        base.update(date=_norm_date(d), note=str(item.get("note", "")).strip())
    elif name == "todos":
        due = str(item.get("due") or "").strip()
        base.update(done=bool(item.get("done", False)),
                    due=_norm_date(due) if due else "")
    elif name == "appointments":
        base.update(when=_norm_when(str(item.get("when") or "").strip()),
                    location=str(item.get("location", "")).strip(),
                    note=str(item.get("note", "")).strip(),
                    recur=_norm_recur(item.get("recur")))
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
                        # structured field — validate so a bad ui/llm value can't
                        # store a malformed rule that breaks expansion later.
                        it[k] = _norm_recur(v) if k == "recur" else v
                found = it
                break
        if found is None:
            return None
        _write_raw(name, items)
        _regen_ics_locked()
    return found


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
        nv = _norm_recur(v) if k == "recur" else (_norm_when(v) if k == "when" else v)
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


def occurrences_in(appt, start_iso, end_iso):
    """when-strings for every time this appointment falls within [start, end]
    (inclusive). non-recurring → its single date if in range. preserves time.
    monthly follows RRULE semantics: anchors on the start day and skips months
    that lack it (jan 31 → mar 31, no feb) so the app matches the phone .ics."""
    when = appt.get("when", "")
    if not when:
        return []
    try:
        anchor = date.fromisoformat(when[:10])
        start = date.fromisoformat(start_iso[:10])
        end = date.fromisoformat(end_iso[:10])
    except ValueError:
        return []
    time_part = when[10:]  # "" for all-day, else "THH:MM"
    recur = appt.get("recur") or ""
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
    names = ["achievements", "todos", "settings"]
    names.append("appointments.cache" if _CALDAV is not None else "appointments")
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


def day(target):
    """all items on a given YYYY-MM-DD date."""
    d = _norm_date(target)
    appts = []
    for a in list_items("appointments"):
        occ = occurrences_in(a, d, d)
        if occ:
            appts.append({**a, "when": occ[0]})  # show the occurrence, not the anchor
    return {
        "date": d,
        "appointments": appts,
        "todos": [t for t in list_items("todos") if t.get("due") == d],
        "achievements": [a for a in list_items("achievements") if a.get("date") == d],
    }


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
    lines = ["BEGIN:VEVENT", f"UID:{uid}@lifeplanner"]
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
        if not due or td.get("done"):
            continue
        try:
            dt = date.fromisoformat(due)
        except ValueError:
            continue
        out += _vevent(td.get("id", ""), "todo: " + td.get("title", ""), dt, True, "", "")
    out.append("END:VCALENDAR")
    return "\r\n".join(out) + "\r\n"


def _write_ics(dst, blob):
    # write bytes (not text) so windows text-mode doesn't turn the RFC5545 \r\n
    # line endings into \r\r\n. atomic via temp + replace.
    tmp = dst.with_suffix(".tmp")
    tmp.write_bytes(blob)
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
