"""shared data layer for lifeplanner.

single source of truth touched by both app.pyw (web ui) and mcp_server.py (claude).
local json files, atomic writes, cross-process lockfile, .ics generation. stdlib only.
"""

import json
import os
import stat
import time
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

__version__ = "1.0.0"

BASE = Path(__file__).resolve().parent
# data dir is configurable so the app is portable (clone-and-run, or point at a
# synced/XDG location). everything generated lives here and is gitignored.
DATA = Path(os.environ.get("LIFEPLANNER_DATA") or (BASE / "data")).expanduser()
LOCK = DATA / ".lock"
ICS = DATA / "lifeplanner.ics"

ENTITIES = ("achievements", "todos", "appointments")
# fields a PATCH is allowed to touch, per entity — anything else is dropped so a
# stray ui/llm key can't pollute stored items. id/created are never patchable.
PATCHABLE = {
    "achievements": ("title", "date", "note"),
    "todos": ("title", "done", "due"),
    "appointments": ("title", "when", "location", "note"),
}
DEFAULT_SETTINGS = {"theme": "dark", "accent": "#ff8700", "ics_sync_path": ""}


def _ensure():
    DATA.mkdir(parents=True, exist_ok=True)


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
    os.replace(tmp, p)  # atomic on the same filesystem


# ---- entities ---------------------------------------------------------------

def list_items(name):
    if name not in ENTITIES:
        raise ValueError(f"unknown entity: {name}")
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
                    note=str(item.get("note", "")).strip())
    return base


def add_item(name, item):
    new = _normalize(name, item)
    with FileLock():
        items = list_items(name)
        items.append(new)
        _write_raw(name, items)
        _regen_ics_locked()
    return new


def update_item(name, item_id, patch):
    with FileLock():
        items = list_items(name)
        found = None
        for it in items:
            if it.get("id") == item_id:
                allowed = PATCHABLE.get(name, ())
                for k, v in patch.items():
                    if k in allowed:
                        it[k] = v
                found = it
                break
        if found is None:
            return None
        _write_raw(name, items)
        _regen_ics_locked()
    return found


def delete_item(name, item_id):
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
        "settings": get_settings(),
        "version": version(),
    }


def version():
    """max mtime (ns) across data files — cheap change token for the ui poller.
    nanosecond resolution so two quick writes never collapse to the same token."""
    latest = 0
    for name in ENTITIES + ("settings",):
        p = _path(name)
        try:
            latest = max(latest, p.stat().st_mtime_ns)
        except OSError:
            pass
    return str(latest)


def day(target):
    """all items on a given YYYY-MM-DD date."""
    d = _norm_date(target)
    return {
        "date": d,
        "appointments": [a for a in list_items("appointments") if when_date(a.get("when")) == d],
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


def _vevent(uid, summary, dtstart, all_day, desc, location):
    lines = ["BEGIN:VEVENT", f"UID:{uid}@lifeplanner"]
    if all_day:
        lines.append(f"DTSTART;VALUE=DATE:{dtstart:%Y%m%d}")
    else:
        # floating local time (no TZID) — matches "when" with no zone info
        lines.append(f"DTSTART:{dtstart:%Y%m%dT%H%M%S}")
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
                       all_day, ap.get("note", ""), ap.get("location", ""))
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
