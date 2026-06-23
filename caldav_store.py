"""appointments backend over CalDAV (radicale) — lifeplanner's optional sync layer.

single source of truth = the caldav collection, shared with the phone (DAVx5) over
a private network. transport is stdlib http.client (radicale's REST is simple: REPORT to
list, PUT to write, DELETE to remove); the vetted `icalendar` lib does the one hard
part — parsing events the phone creates. store.py falls back to a local cache when
the server is unreachable, so the desktop never blanks.

config lives in a private, gitignored `.caldav.json` (url/user/pass). absent → the
app runs in plain local-json mode (zero infra), so the open-source default is intact.
"""

import base64
import http.client
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree.ElementTree import ParseError

import defusedxml.ElementTree as ET
from defusedxml.common import DefusedXmlException
from icalendar import Calendar, Event

BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / ".caldav.json"
RECUR_FREQS = ("daily", "weekly", "monthly")
_NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav"}

# a calendar-query REPORT asking for every VEVENT with its etag + data
_REPORT_BODY = (
    '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
    '<c:filter><c:comp-filter name="VCALENDAR">'
    '<c:comp-filter name="VEVENT"/></c:comp-filter></c:filter>'
    "</c:calendar-query>"
)


class CalDAVError(Exception):
    """raised on transport/protocol failure so store.py can fall back to cache."""


def config():
    """parsed .caldav.json dict, or None when not configured (local mode)."""
    try:
        cfg = json.loads(CONFIG_FILE.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not (cfg.get("url") and cfg.get("user") and cfg.get("pass")):
        return None
    return cfg


# ---- transport --------------------------------------------------------------

def _parts(cfg):
    u = urlsplit(cfg["url"])
    base_path = u.path if u.path.endswith("/") else u.path + "/"
    return u.hostname, (u.port or (443 if u.scheme == "https" else 80)), u.scheme, base_path


def _request(cfg, method, path, body=None, extra=None, timeout=12):
    host, port, scheme, _ = _parts(cfg)
    conn_cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    token = base64.b64encode(f"{cfg['user']}:{cfg['pass']}".encode()).decode()
    headers = {"Authorization": f"Basic {token}"}
    if extra:
        headers.update(extra)
    conn = conn_cls(host, port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, data
    except (OSError, http.client.HTTPException) as e:
        raise CalDAVError(f"{method} {path}: {e}") from e
    finally:
        conn.close()


# ---- mapping: appointment dict <-> iCalendar VEVENT -------------------------

def _local_when(dtval):
    """an icalendar dtstart value -> lifeplanner 'when' string.
    date -> 'YYYY-MM-DD'; datetime -> 'YYYY-MM-DDTHH:MM' in local time (tz-aware
    values are converted, since lifeplanner stores floating local time)."""
    if isinstance(dtval, datetime):
        if dtval.tzinfo is not None:
            dtval = dtval.astimezone().replace(tzinfo=None)
        return dtval.strftime("%Y-%m-%dT%H:%M")
    return dtval.isoformat()


def _parse_rrule(rrule):
    """icalendar vRecur -> lifeplanner recur dict, or '' for any rule richer than
    lifeplanner's model (FREQ + INTERVAL + UNTIL only). a phone rule with BYDAY /
    COUNT / etc. returns '' so it shows as a single event and is NEVER re-modeled
    or destroyed on write-back — the raw event is preserved verbatim instead."""
    if not rrule:
        return ""
    # WKST (week-start) is harmless metadata; anything else we don't model = bail
    if set(rrule.keys()) - {"FREQ", "INTERVAL", "UNTIL", "WKST"}:
        return ""
    freq = str(rrule.get("FREQ", [""])[0]).lower()
    if freq not in RECUR_FREQS:
        return ""
    try:
        interval = max(1, int(rrule.get("INTERVAL", [1])[0]))
    except (TypeError, ValueError):
        interval = 1
    until = ""
    if rrule.get("UNTIL"):
        u = rrule["UNTIL"][0]
        until = (u.date() if isinstance(u, datetime) else u).isoformat()
    return {"freq": freq, "interval": interval, "until": until}


def _event_to_appt(comp, href, etag, raw_ical=""):
    uid = str(comp.get("uid", ""))
    # lifeplanner-origin events carry our 12-hex id in the UID; phone events get a
    # stable id derived from the resource path so edits/deletes can round-trip.
    if uid.endswith("@lifeplanner"):
        item_id = uid.split("@", 1)[0]
    else:
        # full filename stem (DAVx5 uses ~36-char uuids — never truncate, or
        # distinct events could collapse to the same id and edits hit the wrong one)
        item_id = Path(urlsplit(href).path).stem or uid
    dtstart = comp.get("dtstart")
    if dtstart is None:
        return None
    created = ""
    if comp.get("dtstamp"):
        created = _local_when(comp["dtstamp"].dt)
    # end time, from DTEND (preferred) or DURATION. DTEND is EXCLUSIVE per RFC5545,
    # so an all-day end is the day AFTER the last day — store the inclusive last day.
    end = ""
    dtend = comp.get("dtend")
    if dtend is not None:
        ev_end = dtend.dt
        if isinstance(ev_end, datetime):
            end = _local_when(ev_end)
        else:
            end = (ev_end - timedelta(days=1)).isoformat()
    elif comp.get("duration") is not None:
        try:
            end = _local_when(dtstart.dt + comp["duration"].dt)
        except (TypeError, ValueError):
            end = ""
    return {
        "id": item_id,
        "title": str(comp.get("summary", "")).strip() or "(untitled)",
        "when": _local_when(dtstart.dt),
        "end": end,
        "location": str(comp.get("location", "")).strip(),
        "note": str(comp.get("description", "")).strip(),
        "recur": _parse_rrule(comp.get("rrule")),
        "created": created,
        "_href": urlsplit(href).path,
        "_etag": etag,
        "_uid": uid,
        # full original event kept so edits patch in place rather than rebuilding
        # from lifeplanner's simpler model (preserves tz, complex RRULEs, alarms…)
        "_raw": raw_ical,
    }


def _appt_to_ical(appt):
    cal = Calendar()
    cal.add("prodid", "-//lifeplanner//EN")
    cal.add("version", "2.0")
    ev = Event()
    ev.add("uid", appt.get("_uid") or f"{appt['id']}@lifeplanner")
    ev.add("summary", appt.get("title", ""))
    when = appt.get("when", "")
    if len(when) <= 10:
        ev.add("dtstart", date.fromisoformat(when[:10]))
    else:
        ev.add("dtstart", datetime.fromisoformat(when))
    end = appt.get("end") or ""
    if end:
        # DTEND is exclusive — all-day end is the day after the inclusive last day
        if len(when) <= 10:
            ev.add("dtend", date.fromisoformat(end[:10]) + timedelta(days=1))
        else:
            ev.add("dtend", datetime.fromisoformat(end))
    if appt.get("location"):
        ev.add("location", appt["location"])
    if appt.get("note"):
        ev.add("description", appt["note"])
    recur = appt.get("recur") or ""
    if recur:
        rule = {"freq": recur["freq"].upper(), "interval": recur.get("interval", 1)}
        if recur.get("until"):
            rule["until"] = date.fromisoformat(recur["until"])
        ev.add("rrule", rule)
    # no embedded VALARMs: appointment alarms are delivered by reminders.py over a
    # single transport (ntfy → desktop popup), the channel mellen actually reacts to.
    # a second on-device VALARM would double-alert and drift from reminders.py offsets.
    cal.add_component(ev)
    return cal.to_ical()


# ---- operations -------------------------------------------------------------

def list_appointments(cfg):
    """every appointment on the server, with _href/_etag for later edits.
    raises CalDAVError on any transport/protocol failure (caller uses cache)."""
    _, _, _, base_path = _parts(cfg)
    # short timeout so a hung/unreachable server degrades to cache fast instead of
    # freezing the ui — a live local/tailnet server answers in well under a second
    status, data = _request(cfg, "REPORT", base_path, body=_REPORT_BODY,
                            extra={"Depth": "1", "Content-Type": "application/xml"},
                            timeout=6)
    if status not in (207, 200):
        raise CalDAVError(f"REPORT returned {status}")
    try:
        root = ET.fromstring(data)  # defused: blocks XXE / billion-laughs
    except (ParseError, DefusedXmlException) as e:
        raise CalDAVError(f"bad multistatus xml: {e}") from e
    out = []
    for resp in root.findall("d:response", _NS):
        href = resp.findtext("d:href", default="", namespaces=_NS)
        etag = resp.findtext(".//d:getetag", default="", namespaces=_NS) or ""
        caldata = resp.findtext(".//c:calendar-data", default="", namespaces=_NS)
        if not caldata:
            continue
        try:
            cal = Calendar.from_ical(caldata)
        except Exception:  # never let one malformed event break the whole list
            continue
        for comp in cal.walk("VEVENT"):
            appt = _event_to_appt(comp, href, etag.strip('"') if etag else "", caldata)
            if appt:
                out.append(appt)
    return out


_CTAG_BODY = (
    '<propfind xmlns="DAV:" xmlns:cs="http://calendarserver.org/ns/">'
    "<prop><cs:getctag/></prop></propfind>"
)


def collection_ctag(cfg, timeout=5):
    """the collection's change tag — a cheap token that flips whenever ANY event
    changes (incl. from the phone). returns the ctag, or None if unreachable.
    used as a light poll signal so phone-side edits show up live, without a full
    REPORT every few seconds."""
    _, _, _, base_path = _parts(cfg)
    try:
        status, data = _request(cfg, "PROPFIND", base_path, body=_CTAG_BODY,
                                extra={"Depth": "0", "Content-Type": "application/xml"},
                                timeout=timeout)
    except CalDAVError:
        return None
    if status not in (207, 200):
        return None
    try:
        root = ET.fromstring(data)
    except (ParseError, DefusedXmlException):
        return None
    el = root.find(".//{http://calendarserver.org/ns/}getctag")
    return el.text if el is not None and el.text else None


def _resolve_href(cfg, item_id):
    """find the resource path for an existing appointment id, or None."""
    for a in list_appointments(cfg):
        if a["id"] == item_id:
            return a["_href"]
    return None


def _safe_path(base_path, path):
    """reject a resource path that escapes the configured collection — guards
    against a hostile/compromised server handing back a traversing href."""
    # decode first so percent-encoded traversal (%2F..%2F) can't slip past.
    decoded = unquote(path)
    if (not path.startswith(base_path)
            or "/../" in decoded or decoded.endswith("/..") or ".." in decoded.split("/")):
        raise CalDAVError(f"href outside collection: {path}")
    return path


def _set(comp, prop, value):
    if prop in comp:
        del comp[prop]
    if value:
        comp.add(prop, value)


def _set_dtstart(comp, when):
    if "dtstart" in comp:
        del comp["dtstart"]
    if len(when) <= 10:
        comp.add("dtstart", date.fromisoformat(when[:10]))
    else:
        comp.add("dtstart", datetime.fromisoformat(when))


def _set_dtend(comp, when, end):
    """rewrite DTEND from lifeplanner's `end` (or remove it). drops any DURATION too
    so an event never carries both (RFC5545 forbids it). all-day end is exclusive."""
    for k in ("dtend", "duration"):
        if k in comp:
            del comp[k]
    if not end:
        return
    if len(when) <= 10:
        comp.add("dtend", date.fromisoformat(end[:10]) + timedelta(days=1))
    else:
        comp.add("dtend", datetime.fromisoformat(end))


def _set_rrule(comp, recur):
    if "rrule" in comp:
        del comp["rrule"]
    if recur:
        rule = {"freq": recur["freq"].upper(), "interval": recur.get("interval", 1)}
        if recur.get("until"):
            rule["until"] = date.fromisoformat(recur["until"])
        comp.add("rrule", rule)


def _patch_raw(raw, appt, changed):
    """rewrite only the fields the user actually changed into the original event,
    leaving every other property (tz, unmodeled RRULEs, alarms…) untouched."""
    cal = Calendar.from_ical(raw)
    ev = next((c for c in cal.walk("VEVENT")), None)
    if ev is None:
        return _appt_to_ical(appt)
    touch = (lambda k: True) if changed is None else (lambda k: k in changed)
    if touch("title"):
        _set(ev, "summary", appt.get("title", ""))
    if touch("location"):
        _set(ev, "location", appt.get("location", ""))
    if touch("note"):
        _set(ev, "description", appt.get("note", ""))
    if touch("when"):
        _set_dtstart(ev, appt["when"])
    # a changed start OR end must redraw DTEND (moving the start shifts the block)
    if touch("when") or touch("end"):
        _set_dtend(ev, appt.get("when", ""), appt.get("end") or "")
    if touch("recur"):
        _set_rrule(ev, appt.get("recur") or "")
    return cal.to_ical()


def put_appointment(cfg, appt, changed=None):
    """create or replace an appointment. existing events are patched in place
    (only `changed` fields touched) so server-side data lifeplanner doesn't model
    survives; new (lifeplanner-origin) events are built fresh at {id}.ics."""
    _, _, _, base_path = _parts(cfg)
    body = _patch_raw(appt["_raw"], appt, changed) if appt.get("_raw") else _appt_to_ical(appt)
    path = _safe_path(base_path, appt.get("_href") or f"{base_path}{appt['id']}.ics")
    status, _ = _request(cfg, "PUT", path, body=body,
                         extra={"Content-Type": "text/calendar; charset=utf-8"})
    if status not in (200, 201, 204):
        raise CalDAVError(f"PUT returned {status}")
    return True


def delete_appointment(cfg, item_id, href=None):
    _, _, _, base_path = _parts(cfg)
    path = href or _resolve_href(cfg, item_id)
    if not path:
        return False
    status, _ = _request(cfg, "DELETE", _safe_path(base_path, path))
    return status in (200, 204, 404)
