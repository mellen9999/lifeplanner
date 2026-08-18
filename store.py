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


class DuplicateError(ValueError):
    """an appointment add landed on an exact date+time slot that's already
    booked — almost always the same event under different wording (manual add
    vs email auto-add). callers offer an explicit override (force) for the rare
    genuine double-booking."""

    def __init__(self, existing):
        self.existing = existing
        when = (existing.get("when") or "").replace("T", " ")
        super().__init__(f'already booked: "{existing.get("title")}" @ {when}')


# whether the last appointments read came live from the server or fell back to
# cache — surfaced to the ui so it never silently shows stale data as current.
_appt_source = "local"


def appointments_status():
    if _CALDAV is None:
        return {"backend": "local", "source": "local"}
    return {"backend": "caldav", "source": _appt_source}

ENTITIES = ("achievements", "todos", "appointments", "journal")
# fields a PATCH is allowed to touch, per entity — anything else is dropped so a
# stray ui/llm key can't pollute stored items. id/created are never patchable.
PATCHABLE = {
    "achievements": ("title", "date", "note"),
    "todos": ("title", "done", "due", "recur", "order"),
    "appointments": ("title", "when", "end", "location", "note", "recur"),
    # a diary entry: free-text `body` stamped to a moment (`when`, backdatable).
    # no title — a brain-dump is just what happened, not a headline.
    "journal": ("body", "when"),
}
# each entity's one required, non-empty text field — its primary content. a save
# with this blank is rejected loudly (a titleless win or bodyless diary entry is
# meaningless). the single place "what makes an item real" is declared.
REQUIRED = {
    "achievements": "title",
    "todos": "title",
    "appointments": "title",
    "journal": "body",
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
    if name == "appointments" and isinstance(value, list):
        # `hidden` is a read-time annotation from the overlay file — never persist
        # it into the source-of-truth list, or the two could silently drift.
        value = [{k: v for k, v in it.items() if k != "hidden"}
                 if isinstance(it, dict) else it for it in value]
    p = _path(name)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), "utf-8")
    try:
        tmp.chmod(0o600)  # private — these files carry health/legal titles
    except OSError:
        pass
    os.replace(tmp, p)  # atomic on the same filesystem


# ---- hidden (muted) appointments --------------------------------------------
# a recurring series synced in from the phone (e.g. a daily alarm) can spam every
# calendar day. "hidden" mutes it: dropped from the calendar/today/ics/reminder
# surfaces, still listed (dimmed) on the appointments page. the flag lives in a
# local overlay file keyed by appointment id — the caldav server and the phone's
# own events are never touched, so muting can't corrupt or lose an event.

def _hidden_ids():
    try:
        ids = _read_raw("hidden_appts", [])
    except OSError:
        return set()  # overlay is best-effort; a read fault just means "none hidden"
    return {x for x in ids if isinstance(x, str)} if isinstance(ids, list) else set()


def set_hidden(item_id, hidden):
    """add/remove one appointment id in the hidden overlay + refresh the .ics feed."""
    with FileLock():
        ids = _hidden_ids()
        ids.add(item_id) if hidden else ids.discard(item_id)
        _write_raw("hidden_appts", sorted(ids))
        _regen_ics_locked()


# ---- entities ---------------------------------------------------------------

# ---- caldav-backed appointments ---------------------------------------------

def _cache_write(items):
    # skip the write when nothing changed: version() reads this file's mtime as a
    # change signal, and every state fetch refreshes the cache — an unconditional
    # write would flip the version on each poll, putting the ui in a permanent
    # refresh loop (repainting every 4s and wiping half-typed add forms).
    try:
        blob = json.dumps(items, indent=2, ensure_ascii=False)
        p = _path("appointments.cache")
        if p.exists() and p.read_text("utf-8") == blob:
            return
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
        items = _caldav_list()
    else:
        items = _read_raw(name, [])
        if not isinstance(items, list):
            return []
        # drop any non-dict element so a poisoned/partially-written file can't crash
        # every caller that does item.get(...) downstream (state, day, occurrences).
        items = [it for it in items if isinstance(it, dict)]
    if name == "appointments":
        hid = _hidden_ids()
        for it in items:
            it["hidden"] = it.get("id") in hid
    return items


def _coerce(name, key, value):
    """normalize one field to its stored form. the single place field validation
    lives — so add and every update path coerce identically and can never drift
    (a bad date/rule can't slip in through one door and crash expansion later)."""
    if key == "recur":
        return _norm_recur(value)
    if key == "when":
        return _norm_when(str(value or "").strip())
    if key == "end":
        s = str(value or "").strip()
        return _norm_when(s) if s else ""
    if key == "due":
        s = str(value or "").strip()
        return _norm_date(s) if s else ""
    if key == "date":
        return _norm_date(str(value or "").strip())
    if key in ("title", "location", "note", "body"):
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
    """coerce + default every field so the stored item is well-formed. loud when the
    entity's required field (title, or body for the diary) is empty."""
    req = REQUIRED.get(name, "title")
    primary = _coerce(name, req, item.get(req))
    if not primary:
        raise ValueError(f"{req} is required")
    base = {"id": uuid4().hex[:12], req: primary,
            "created": datetime.now().isoformat(timespec="seconds")}
    for key in PATCHABLE[name]:
        if key != req:
            base[key] = _coerce(name, key, item.get(key))
    # a diary entry stamps to the moment it's written (with time) unless the caller
    # backdated it — _norm_when drops the clock on an empty value, which would lose
    # the time-of-day a log is meant to keep, so fill now() here instead.
    if name == "journal" and not str(item.get("when") or "").strip():
        base["when"] = datetime.now().isoformat(timespec="minutes")
    # an appointment's end only counts if it's a real span after the start
    if name == "appointments":
        base["end"] = _reconcile_end(base.get("when", ""), base.get("end", ""))
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


def _slot_conflict(new):
    """the existing appointment occupying new's exact date+time slot, or None.
    date-only events have no slot and never conflict."""
    when = (new.get("when") or "").replace("T", " ")[:16]
    if len(when) < 16:
        return None
    for a in list_items("appointments"):
        if (a.get("when") or "").replace("T", " ")[:16] == when:
            return a
    return None


def add_item(name, item, force=False):
    # `force` may also arrive inside the payload (web/mcp callers) — peel it off
    # so it's never stored. it bypasses only the duplicate-slot guard.
    force = force or bool(item.get("force"))
    item = {k: v for k, v in item.items() if k != "force"}
    new = _normalize(name, item)
    if name == "appointments" and not force:
        dupe = _slot_conflict(new)
        if dupe is not None:
            raise DuplicateError(dupe)
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
    # `hidden` is overlay-only, never a stored/caldav field — peel it off here so
    # muting works identically in both backends and can't be written to the server.
    if name == "appointments" and "hidden" in patch:
        patch = dict(patch)
        hidden = bool(patch.pop("hidden"))
        cur = next((a for a in list_items(name) if a.get("id") == item_id), None)
        if cur is None:
            return None
        set_hidden(item_id, hidden)
        if not patch:  # hidden was the whole edit — done
            cur["hidden"] = hidden
            return cur
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
                if name == "appointments":
                    it["end"] = _reconcile_end(it.get("when", ""), it.get("end", ""))
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
    # keep end a valid span after start; a moved start must also redraw the end block
    recon = _reconcile_end(cur.get("when", ""), cur.get("end", ""))
    if recon != cur.get("end", ""):
        cur["end"] = recon
        changed.add("end")
    if "when" in changed and cur.get("end"):
        changed.add("end")
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
        if ok and item_id in _hidden_ids():
            set_hidden(item_id, False)  # prune the overlay so it can't grow stale
        return ok
    with FileLock():
        items = list_items(name)
        kept = [it for it in items if it.get("id") != item_id]
        if len(kept) == len(items):
            return False
        _write_raw(name, kept)
        _regen_ics_locked()
    if name == "appointments" and item_id in _hidden_ids():
        set_hidden(item_id, False)  # prune the overlay so it can't grow stale
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


# ---- coach directive --------------------------------------------------------
# ONE claude-written "next optimal move" line, written by brief.py and shown
# beside the calendar. the store owns its lifecycle so the writer can behave like
# a service instead of a broadcaster: the state fingerprint the line was written
# for (stay silent while nothing has changed), the recent lines and whether each
# was dismissed (never re-nag), and a dismissal flag (mellen kills a line and it
# stays dead until the state actually moves).

COACH_RECENT_MAX = 5  # recent lines shown to the writer as "you already said these"


def get_coach():
    c = _read_raw("coach", {})
    return c if isinstance(c, dict) else {}


def coach_is_current(fp):
    """true when a live directive was already written for this exact state — the
    writer's silence gate. no stored fingerprint (pre-lifecycle data) reads as
    stale, so the first run after an upgrade re-judges once."""
    c = get_coach()
    return bool(fp and c.get("line") and c.get("fp") == fp)


def coach_recent_lines():
    """the last few directives, newest last, each with whether it was dismissed."""
    r = get_coach().get("recent", [])
    return [x for x in r if isinstance(x, dict) and x.get("line")]


def set_coach(line, fp=""):
    """store a NEW directive: resets the age, clears any dismissal, and files the
    previous line under `recent`. an identical line only records the fingerprint —
    a re-say is not news, so it must never flip the age or un-dismiss. returns the
    stored dict, or None if the line was blank."""
    line = " ".join(str(line or "").split())
    if not line:
        return None
    with FileLock():
        cur = get_coach()
        if cur.get("line") == line:
            if cur.get("fp") == fp:
                return cur  # nothing to write — keeps the version token still
            obj = {**cur, "fp": fp}
        else:
            recent = coach_recent_lines()
            if cur.get("line"):
                recent.append({"line": cur["line"], "dismissed": bool(cur.get("dismissed"))})
            obj = {"line": line, "created": datetime.now().isoformat(timespec="minutes"),
                   "fp": fp, "recent": recent[-COACH_RECENT_MAX:]}
        _write_raw("coach", obj)
    return obj


def touch_coach(fp):
    """record that this state was judged and yielded nothing new — keeps the line,
    its age and any dismissal, and stops the writer re-asking until the state
    moves again."""
    with FileLock():
        cur = get_coach()
        if not cur.get("line") or cur.get("fp") == fp:
            return cur
        obj = {**cur, "fp": fp}
        _write_raw("coach", obj)
    return obj


def dismiss_coach():
    """mellen kills the current line. it stays hidden until a new one is written,
    and the writer is told it was rejected so it doesn't rephrase the same nag."""
    with FileLock():
        cur = get_coach()
        if not cur.get("line") or cur.get("dismissed"):
            return False
        _write_raw("coach", {**cur, "dismissed": True})
    return True


def coach_push_pending():
    """the live line if it still owes an outbound push, else "" — a dismissed or
    already-sent line never does. read-only on purpose: the sender claims the
    line only once the push actually landed, so a dead ntfy retries next run."""
    c = get_coach()
    line = c.get("line", "")
    return "" if not line or c.get("dismissed") or c.get("pushed") == line else line


def mark_coach_pushed():
    """claim the current line for one outbound push. false when there is nothing
    live to send or the line already went out — the push channel never repeats."""
    with FileLock():
        cur = get_coach()
        line = cur.get("line", "")
        if not line or cur.get("dismissed") or cur.get("pushed") == line:
            return False
        _write_raw("coach", {**cur, "pushed": line})
    return True


# ---- coach chat + memory ----------------------------------------------------
# the coach's persistence. every chat turn is kept append-only (nothing mellen
# tells the coach is ever lost), and durable facts the coach distils live in a
# separate memory list. both feed the coach prompt so it never starts cold.

COACH_MSG_MAX = 4000      # prompt window per HISTORY turn — never a persistence cap
COACH_TURN_MAX = 64_000   # persistence cap per turn (a runaway-paste bound, not a trim)
COACH_NOTE_MAX = 500      # one memory note
COACH_MEM_BUDGET = 6_000  # chars of memory notes inlined into any coach prompt


def log_coach_turn(role, text):
    """append one chat turn (role: 'you' | 'coach' | 'system'). 'system' marks a
    failed coach turn so the message above it never reads as silently answered.
    returns the stored turn (with "clipped": True if the text was actually cut),
    or None for blank/invalid input."""
    text = str(text or "").strip()
    if not text or role not in ("you", "coach", "system"):
        return None
    turn = {"role": role, "text": text[:COACH_TURN_MAX],
            "ts": datetime.now().isoformat(timespec="minutes")}
    if len(text) > COACH_TURN_MAX:
        turn["clipped"] = True
    with FileLock():
        turns = _read_raw("coach_chat", [])
        if not isinstance(turns, list):
            turns = []
        turns.append(turn)
        _write_raw("coach_chat", turns)
    return turn


def coach_chat_tail(n=12):
    """the most recent n chat turns, oldest first."""
    turns = _read_raw("coach_chat", [])
    if not isinstance(turns, list):
        return []
    return [t for t in turns[-max(1, n):] if isinstance(t, dict)]


def coach_chat_page(limit=40, before_index=-1):
    """a stable page of the full transcript for outside readers (the mcp tool).
    turns carry their absolute index `i` into the append-only list, so paging
    back with before_index=<smallest i seen> can't shift under a concurrent
    append. returns {"total": N, "turns": [...oldest-first...]}."""
    turns = _read_raw("coach_chat", [])
    if not isinstance(turns, list):
        turns = []
    total = len(turns)
    limit = max(1, min(int(limit or 40), 200))
    end = total if before_index is None or before_index < 0 else min(int(before_index), total)
    page = [{"i": i, **t} for i, t in enumerate(turns[max(0, end - limit):end], start=max(0, end - limit))
            if isinstance(t, dict)]
    return {"total": total, "turns": page}


def _ensure_memory_ids(notes):
    """give id-less notes (the pre-id era) stable ids. returns True if any were
    assigned — caller persists. runs inside the lock."""
    changed = False
    for n in notes:
        if isinstance(n, dict) and n.get("note") and not n.get("id"):
            n["id"] = uuid4().hex[:12]
            changed = True
    return changed


def add_coach_memory(note, on=None):
    """save one durable fact about mellen. whitespace-collapsed, length-capped.
    an exact (normalized) duplicate returns the existing entry without appending —
    the deterministic backstop that lets remember + the nightly distiller overlap
    safely. `on` optionally backdates (YYYY-MM-DD) — used by undo to keep a
    restored note's original date."""
    note = " ".join(str(note or "").split())[:COACH_NOTE_MAX]
    if not note:
        return None
    with FileLock():
        notes = _read_raw("coach_memory", [])
        if not isinstance(notes, list):
            notes = []
        migrated = _ensure_memory_ids(notes)
        dup = next((n for n in notes if isinstance(n, dict) and n.get("note") == note), None)
        if dup:
            if migrated:
                _write_raw("coach_memory", notes)
            return dup
        entry = {"id": uuid4().hex[:12], "note": note,
                 "date": _norm_date(on) if on else date.today().isoformat()}
        notes.append(entry)
        _write_raw("coach_memory", notes)
    return entry


def list_coach_memory():
    notes = _read_raw("coach_memory", [])
    if not isinstance(notes, list):
        return []
    valid = [n for n in notes if isinstance(n, dict) and n.get("note")]
    # lazy one-time migration of the pre-id era: double-checked so the steady
    # state stays a lock-free read.
    if any(not n.get("id") for n in valid):
        with FileLock():
            notes = _read_raw("coach_memory", [])
            if isinstance(notes, list) and _ensure_memory_ids(notes):
                _write_raw("coach_memory", notes)
            valid = [n for n in notes if isinstance(n, dict) and n.get("note")]
    return valid


def delete_coach_memory(note_id):
    """remove one memory note by id. returns True on a hit; a miss writes
    nothing (so it can't flip the version token)."""
    if not note_id:
        return False
    with FileLock():
        notes = _read_raw("coach_memory", [])
        if not isinstance(notes, list):
            return False
        kept = [n for n in notes if not (isinstance(n, dict) and n.get("id") == note_id)]
        if len(kept) == len(notes):
            return False
        _write_raw("coach_memory", kept)
    return True


def coach_memory_window(budget=COACH_MEM_BUDGET):
    """the notes every coach prompt inlines: newest kept first under the char
    budget, returned oldest-first for rendering, plus how many older ones were
    left out. ONE window shared by the chat coach and the brief timer, so what
    the coach knows never diverges by consumer."""
    notes = list_coach_memory()
    kept, used = [], 0
    for n in reversed(notes):
        used += len(n.get("note", ""))
        if kept and used > budget:
            break
        kept.append(n)
    kept.reverse()
    return kept, len(notes) - len(kept)


def coach_distill_cursor():
    """how many chat turns the nightly distiller has already consumed."""
    obj = _read_raw("coach_distill", {})
    try:
        return max(0, int(obj.get("cursor", 0))) if isinstance(obj, dict) else 0
    except (TypeError, ValueError):
        return 0


def set_coach_distill_cursor(n):
    with FileLock():
        _write_raw("coach_distill", {"cursor": max(0, int(n)),
                                     "ts": datetime.now().isoformat(timespec="minutes")})


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


def _reconcile_end(when, end):
    """validate an appointment's end against its start. the end must share the
    start's shape (all-day → date, timed → datetime) and fall strictly after it —
    otherwise there's no block to draw, so drop it to "" (a point event). never
    raises: a bad end can't crash a save, it just isn't kept."""
    if not end or not when:
        return ""
    if len(when) <= 10:                       # all-day start
        e = end[:10]
        return e if e > when[:10] else ""     # only keep a genuine multi-day span
    if len(end) <= 10:                        # a timed start needs a timed end
        return ""
    return end if end > when else ""


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


def _coach_view():
    """what the ui gets: the live directive (nothing while it's dismissed), the
    recent chat and the memory notes. the lifecycle fields — fingerprint, recent
    lines, push marker — stay server-side; the ui never has to reason about them."""
    c = get_coach()
    return {"line": "" if c.get("dismissed") else c.get("line", ""),
            "created": c.get("created", ""),
            "chat": coach_chat_tail(12),
            "memory": list_coach_memory()}


def state():
    """everything the ui needs in one shot."""
    return {
        "achievements": sorted(list_items("achievements"),
                               key=lambda a: (a.get("date", ""), a.get("created", "")),
                               reverse=True),
        "todos": list_items("todos"),
        "appointments": sorted(list_items("appointments"),
                               key=lambda a: a.get("when", "")),
        "journal": sorted(list_items("journal"),
                          key=lambda j: (j.get("when", ""), j.get("created", "")),
                          reverse=True),
        "sync": appointments_status(),
        "settings": get_settings(),
        "coach": _coach_view(),
        "version": version(),
    }


def version():
    """cheap change token for the ui poller. nanosecond mtimes so two quick writes
    never collapse; in caldav mode it also folds in the server's collection tag so
    a change made on the phone flips the token and the desktop refreshes live."""
    # derived from ENTITIES (+ settings + the hidden overlay, so a mute/unmute
    # refreshes every open ui) — a new entity's writes always flip the token; in
    # caldav mode appointments live on the server, so watch the cache.
    names = ["settings", "hidden_appts", "coach", "coach_chat", "coach_memory"]
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
            d, {"date": d, "appointments": [], "todos": [], "achievements": [], "journal": []})

    # hidden (muted) series are exactly the spam a day view exists to avoid
    for a in list_items("appointments"):
        if a.get("hidden"):
            continue
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
    # diary entries bucket on the date of their `when` (ignoring time-of-day).
    for j in list_items("journal"):
        w = j.get("when", "")
        if w and s <= w[:10] <= e:
            slot(w[:10])["journal"].append(j)
    return out


def day(target):
    """all items on a given YYYY-MM-DD date (the empty shape if nothing falls on it)."""
    d = _norm_date(target)
    return days(d, d).get(d, {"date": d, "appointments": [], "todos": [], "achievements": [], "journal": []})


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


def _vevent(uid, summary, dtstart, all_day, desc, location, recur="", end="", alarms=()):
    # DTSTAMP is REQUIRED by RFC5545 §3.6.1 — strict importers (radicale,
    # thunderbird) reject events without it.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VEVENT", f"UID:{uid}@lifeplanner", f"DTSTAMP:{stamp}"]
    if all_day:
        lines.append(f"DTSTART;VALUE=DATE:{dtstart:%Y%m%d}")
    else:
        # floating local time (no TZID) — matches "when" with no zone info
        lines.append(f"DTSTART:{dtstart:%Y%m%dT%H%M%S}")
    # DTEND is EXCLUSIVE per RFC5545 — an all-day span ends the day AFTER its last
    # day, so a calendar draws the block over the right number of days.
    if end:
        try:
            if all_day:
                ed = date.fromisoformat(end[:10]) + timedelta(days=1)
                lines.append(f"DTEND;VALUE=DATE:{ed:%Y%m%d}")
            else:
                lines.append(f"DTEND:{datetime.fromisoformat(end):%Y%m%dT%H%M%S}")
        except ValueError:
            pass
    rrule = _rrule(recur, all_day)
    if rrule:
        lines.append(rrule)
    lines.append(f"SUMMARY:{_ics_escape(summary)}")
    if desc:
        lines.append(f"DESCRIPTION:{_ics_escape(desc)}")
    if location:
        lines.append(f"LOCATION:{_ics_escape(location)}")
    # local-fire reminders the subscribing calendar schedules on-device
    for trig in alarms:
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY",
                  f"DESCRIPTION:{_ics_escape(summary)}", f"TRIGGER:{trig}", "END:VALARM"]
    lines.append("END:VEVENT")
    return [_fold(x) for x in lines]


def build_ics():
    """appointments + due-dated todos as a read-only VCALENDAR string."""
    out = ["BEGIN:VCALENDAR", "VERSION:2.0",
           "PRODID:-//lifeplanner//EN", "CALSCALE:GREGORIAN",
           "X-WR-CALNAME:lifeplanner"]
    for ap in list_items("appointments"):
        if ap.get("hidden"):
            continue  # muted — keep it out of every subscribed calendar too
        when = ap.get("when", "")
        all_day = len(when) <= 10
        try:
            dt = date.fromisoformat(when[:10]) if all_day else datetime.fromisoformat(when)
        except ValueError:
            continue
        # 1 day before (+ 1 hour before, for timed) — local alarms the phone fires itself
        alarms = ["-P1D"] if all_day else ["-P1D", "-PT1H"]
        out += _vevent(ap.get("id", ""), ap.get("title", "appointment"), dt,
                       all_day, ap.get("note", ""), ap.get("location", ""),
                       ap.get("recur", ""), ap.get("end", ""), alarms)
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
    names = (*ENTITIES, "appointments.cache", "settings", "hidden_appts",
             "coach", "coach_chat", "coach_memory", "coach_distill")
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
