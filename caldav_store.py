"""appointments backend over CalDAV (radicale) — lifeplanner's optional sync layer.

single source of truth = the caldav collection, shared with the phone (DAVx5) over
tailscale. transport is stdlib http.client (radicale's REST is simple: REPORT to
list, PUT to write, DELETE to remove); the vetted `icalendar` lib does the one hard
part — parsing events the phone creates. store.py falls back to a local cache when
the server is unreachable, so the desktop never blanks.

config lives in a private, gitignored `.caldav.json` (url/user/pass). absent → the
app runs in plain local-json mode (zero infra), so the open-source default is intact.
"""

import base64
import http.client
import json
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit
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
    if not (cfg.get("url") and cfg.get("user")):
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
    """icalendar vRecur -> lifeplanner recur dict, or '' for unsupported rules
    (fail safe: an exotic phone RRULE shows as a single event, never crashes)."""
    if not rrule:
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


def _event_to_appt(comp, href, etag):
    uid = str(comp.get("uid", ""))
    # lifeplanner-origin events carry our 12-hex id in the UID; phone events get a
    # stable id derived from the resource path so edits/deletes can round-trip.
    if uid.endswith("@lifeplanner"):
        item_id = uid.split("@", 1)[0]
    else:
        item_id = Path(urlsplit(href).path).stem[:24] or uid[:24]
    dtstart = comp.get("dtstart")
    if dtstart is None:
        return None
    created = ""
    if comp.get("dtstamp"):
        created = _local_when(comp["dtstamp"].dt)
    return {
        "id": item_id,
        "title": str(comp.get("summary", "")).strip() or "(untitled)",
        "when": _local_when(dtstart.dt),
        "location": str(comp.get("location", "")).strip(),
        "note": str(comp.get("description", "")).strip(),
        "recur": _parse_rrule(comp.get("rrule")),
        "created": created,
        "_href": urlsplit(href).path,
        "_etag": etag,
        "_uid": uid,
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
    cal.add_component(ev)
    return cal.to_ical()


# ---- operations -------------------------------------------------------------

def list_appointments(cfg):
    """every appointment on the server, with _href/_etag for later edits.
    raises CalDAVError on any transport/protocol failure (caller uses cache)."""
    _, _, _, base_path = _parts(cfg)
    status, data = _request(cfg, "REPORT", base_path, body=_REPORT_BODY,
                            extra={"Depth": "1", "Content-Type": "application/xml"})
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
            appt = _event_to_appt(comp, href, etag.strip('"') if etag else "")
            if appt:
                out.append(appt)
    return out


def _resolve_href(cfg, item_id):
    """find the resource path for an existing appointment id, or None."""
    for a in list_appointments(cfg):
        if a["id"] == item_id:
            return a["_href"]
    return None


def put_appointment(cfg, appt):
    """create or replace an appointment. lifeplanner-origin events live at
    {id}.ics; phone-origin keep their existing href (carried on the dict)."""
    _, _, _, base_path = _parts(cfg)
    path = appt.get("_href") or f"{base_path}{appt['id']}.ics"
    status, _ = _request(cfg, "PUT", path, body=_appt_to_ical(appt),
                         extra={"Content-Type": "text/calendar; charset=utf-8"})
    if status not in (200, 201, 204):
        raise CalDAVError(f"PUT returned {status}")
    return True


def delete_appointment(cfg, item_id, href=None):
    path = href or _resolve_href(cfg, item_id)
    if not path:
        return False
    status, _ = _request(cfg, "DELETE", path)
    return status in (200, 204, 404)
