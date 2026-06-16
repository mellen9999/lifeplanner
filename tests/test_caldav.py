"""caldav mapping tests — pure functions, no server needed.

covers the risky bits: RRULE <-> recur, timezone/all-day handling, id derivation,
and that editing one field of a phone-made event never destroys the rest of it.
skipped automatically if the optional icalendar dep isn't installed.
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import caldav_store as cd
    from icalendar import Calendar
    HAVE = True
except Exception:
    HAVE = False


def _appt(ics, href="/user/lifeplanner/x.ics", etag="e"):
    cal = Calendar.from_ical(ics)
    comp = next(c for c in cal.walk("VEVENT"))
    return cd._event_to_appt(comp, href, etag, ics)


def _wrap(vevent):
    return ("BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//EN\n"
            + vevent.strip() + "\nEND:VCALENDAR\n")


@unittest.skipUnless(HAVE, "icalendar not installed")
class CalDAVMappingTest(unittest.TestCase):

    # ---- recurrence parsing ----
    def test_weekly_interval(self):
        a = _appt(_wrap("BEGIN:VEVENT\nUID:u@lifeplanner\nDTSTART:20260611T090000\n"
                        "SUMMARY:x\nRRULE:FREQ=WEEKLY;INTERVAL=2\nEND:VEVENT"))
        self.assertEqual(a["recur"], {"freq": "weekly", "interval": 2, "until": ""})

    def test_until(self):
        a = _appt(_wrap("BEGIN:VEVENT\nUID:u@lifeplanner\nDTSTART:20260611\n"
                        "SUMMARY:x\nRRULE:FREQ=WEEKLY;UNTIL=20260701\nEND:VEVENT"))
        self.assertEqual(a["recur"]["until"], "2026-07-01")

    def test_byday_is_unmodeled(self):
        # "every mon & wed" — richer than our model, must NOT be mis-read as weekly
        a = _appt(_wrap("BEGIN:VEVENT\nUID:u@lifeplanner\nDTSTART:20260611T090000\n"
                        "SUMMARY:x\nRRULE:FREQ=WEEKLY;BYDAY=MO,WE\nEND:VEVENT"))
        self.assertEqual(a["recur"], "")

    def test_count_is_unmodeled(self):
        a = _appt(_wrap("BEGIN:VEVENT\nUID:u@lifeplanner\nDTSTART:20260611T090000\n"
                        "SUMMARY:x\nRRULE:FREQ=DAILY;COUNT=5\nEND:VEVENT"))
        self.assertEqual(a["recur"], "")

    # ---- date/time mapping ----
    def test_all_day(self):
        a = _appt(_wrap("BEGIN:VEVENT\nUID:u@lifeplanner\nDTSTART;VALUE=DATE:20260624\n"
                        "SUMMARY:x\nEND:VEVENT"))
        self.assertEqual(a["when"], "2026-06-24")

    def test_timed_naive(self):
        a = _appt(_wrap("BEGIN:VEVENT\nUID:u@lifeplanner\nDTSTART:20260624T093000\n"
                        "SUMMARY:x\nEND:VEVENT"))
        self.assertEqual(a["when"], "2026-06-24T09:30")

    def test_tz_aware_converts_to_local(self):
        # a UTC datetime maps to the machine's local wall-clock time
        dt = datetime(2026, 6, 24, 17, 0, tzinfo=timezone.utc)
        expect = dt.astimezone().strftime("%Y-%m-%dT%H:%M")
        self.assertEqual(cd._local_when(dt), expect)

    # ---- id derivation ----
    def test_id_from_lifeplanner_uid(self):
        a = _appt(_wrap("BEGIN:VEVENT\nUID:abc123def456@lifeplanner\n"
                        "DTSTART:20260101\nSUMMARY:x\nEND:VEVENT"))
        self.assertEqual(a["id"], "abc123def456")

    def test_id_from_href_for_phone_event(self):
        a = _appt(_wrap("BEGIN:VEVENT\nUID:random-phone-uid\nDTSTART:20260101\n"
                        "SUMMARY:x\nEND:VEVENT"),
                  href="/user/lifeplanner/9f8e7d6c-1234.ics")
        self.assertEqual(a["id"], "9f8e7d6c-1234")  # full stem, not truncated

    def test_missing_dtstart_dropped(self):
        cal = Calendar.from_ical(_wrap("BEGIN:VEVENT\nUID:u\nSUMMARY:x\nEND:VEVENT"))
        comp = next(c for c in cal.walk("VEVENT"))
        self.assertIsNone(cd._event_to_appt(comp, "/x.ics", "e", ""))

    # ---- round trip (lifeplanner-origin build) ----
    def test_build_roundtrip(self):
        appt = {"id": "abc123def456", "title": "win", "when": "2026-06-11T09:00",
                "location": "office", "note": "n",
                "recur": {"freq": "weekly", "interval": 2, "until": ""}}
        ical = cd._appt_to_ical(appt)
        back = _appt(ical.decode())
        self.assertEqual(back["title"], "win")
        self.assertEqual(back["when"], "2026-06-11T09:00")
        self.assertEqual(back["location"], "office")
        self.assertEqual(back["recur"], {"freq": "weekly", "interval": 2, "until": ""})

    # ---- minimal patch preserves unmodeled data ----
    def test_patch_preserves_alarm_and_complex_rrule(self):
        raw = _wrap("BEGIN:VEVENT\nUID:p\nDTSTART:20260620T100000\nSUMMARY:old\n"
                    "RRULE:FREQ=WEEKLY;BYDAY=MO,WE\n"
                    "BEGIN:VALARM\nACTION:DISPLAY\nTRIGGER:-PT30M\nEND:VALARM\nEND:VEVENT")
        appt = {"title": "new", "when": "2026-06-20T10:00", "location": "", "note": "",
                "recur": ""}
        out = cd._patch_raw(raw, appt, changed={"title"}).decode()
        self.assertIn("SUMMARY:new", out)
        self.assertIn("BYDAY=MO,WE", out)   # recurrence untouched
        self.assertIn("BEGIN:VALARM", out)  # alarm untouched

    # ---- end time / DTEND ----
    def test_end_timed_roundtrip(self):
        appt = {"id": "abc123def456", "title": "anger mgmt", "when": "2026-06-16T14:00",
                "end": "2026-06-16T15:00", "location": "", "note": "", "recur": ""}
        ical = cd._appt_to_ical(appt)
        self.assertIn("DTEND:20260616T150000", ical.decode())
        self.assertEqual(_appt(ical.decode())["end"], "2026-06-16T15:00")

    def test_end_allday_is_exclusive(self):
        # a 24th–26th span ends (exclusive) on the 27th in iCal, round-trips back to 26th
        appt = {"id": "x", "title": "trip", "when": "2026-06-24", "end": "2026-06-26",
                "location": "", "note": "", "recur": ""}
        ical = cd._appt_to_ical(appt)
        self.assertIn("DTEND;VALUE=DATE:20260627", ical.decode())
        self.assertEqual(_appt(ical.decode())["end"], "2026-06-26")

    def test_duration_parses_to_end(self):
        # a phone may send DURATION instead of DTEND
        a = _appt(_wrap("BEGIN:VEVENT\nUID:d@lifeplanner\nDTSTART:20260616T140000\n"
                        "SUMMARY:x\nDURATION:PT1H\nEND:VEVENT"))
        self.assertEqual(a["end"], "2026-06-16T15:00")

    def test_no_dtend_no_end(self):
        a = _appt(_wrap("BEGIN:VEVENT\nUID:n@lifeplanner\nDTSTART:20260616T140000\n"
                        "SUMMARY:x\nEND:VEVENT"))
        self.assertEqual(a["end"], "")

    def test_patch_sets_and_clears_dtend(self):
        raw = _wrap("BEGIN:VEVENT\nUID:p\nDTSTART:20260616T140000\nSUMMARY:x\nEND:VEVENT")
        appt = {"title": "x", "when": "2026-06-16T14:00", "end": "2026-06-16T15:00",
                "location": "", "note": "", "recur": ""}
        out = cd._patch_raw(raw, appt, changed={"end"}).decode()
        self.assertIn("DTEND:20260616T150000", out)
        appt["end"] = ""
        out2 = cd._patch_raw(out, appt, changed={"end"}).decode()
        self.assertNotIn("DTEND", out2)

    # ---- path safety ----
    def test_safe_path_rejects_traversal(self):
        with self.assertRaises(cd.CalDAVError):
            cd._safe_path("/user/lifeplanner/", "/user/lifeplanner/../../etc/x.ics")

    def test_safe_path_rejects_encoded_traversal(self):
        # a hostile server could percent-encode the traversal to slip past a naive check
        with self.assertRaises(cd.CalDAVError):
            cd._safe_path("/user/lifeplanner/", "/user/lifeplanner/%2e%2e/%2e%2e/etc/x.ics")

    def test_safe_path_rejects_outside(self):
        with self.assertRaises(cd.CalDAVError):
            cd._safe_path("/user/lifeplanner/", "/other/x.ics")

    def test_safe_path_allows_inside(self):
        p = "/user/lifeplanner/abc.ics"
        self.assertEqual(cd._safe_path("/user/lifeplanner/", p), p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
