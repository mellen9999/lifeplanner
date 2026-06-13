"""data-layer tests. stdlib unittest only (no mcp needed).

run:  python3 -m unittest discover -s tests
each run uses a throwaway data dir so it never touches real data.
"""

import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

# isolate the data dir BEFORE importing store (store reads the env at import time)
os.environ["LIFEPLANNER_DATA"] = tempfile.mkdtemp(prefix="lp-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store  # noqa: E402


class StoreTest(unittest.TestCase):
    def setUp(self):
        for name in store.ENTITIES + ("settings",):
            p = store._path(name)
            if p.exists():
                p.unlink()

    # ---- create / read ----
    def test_add_and_list(self):
        store.add_item("achievements", {"title": "did a thing"})
        items = store.list_items("achievements")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "did a thing")
        self.assertEqual(len(items[0]["id"]), 12)

    def test_title_required(self):
        with self.assertRaises(ValueError):
            store.add_item("todos", {"title": "   "})

    def test_unknown_entity_rejected(self):
        with self.assertRaises(ValueError):
            store.list_items("nope")

    # ---- defaults / normalization ----
    def test_achievement_date_defaults_today(self):
        a = store.add_item("achievements", {"title": "x"})
        self.assertEqual(a["date"], date.today().isoformat())

    def test_appointment_keeps_time(self):
        a = store.add_item("appointments", {"title": "dr", "when": "2026-06-24 14:30"})
        self.assertEqual(a["when"], "2026-06-24T14:30")

    def test_bad_date_falls_back(self):
        a = store.add_item("achievements", {"title": "x", "date": "garbage"})
        self.assertEqual(a["date"], date.today().isoformat())

    # ---- update / delete ----
    def test_complete_and_delete_todo(self):
        t = store.add_item("todos", {"title": "task", "due": "2026-06-13"})
        self.assertFalse(t["done"])
        store.update_item("todos", t["id"], {"done": True})
        self.assertTrue(store.list_items("todos")[0]["done"])
        self.assertTrue(store.delete_item("todos", t["id"]))
        self.assertEqual(store.list_items("todos"), [])

    def test_update_cannot_change_id(self):
        t = store.add_item("todos", {"title": "task"})
        store.update_item("todos", t["id"], {"id": "hacked", "created": "nope"})
        got = store.list_items("todos")[0]
        self.assertEqual(got["id"], t["id"])

    def test_update_drops_unknown_keys(self):
        t = store.add_item("todos", {"title": "task"})
        store.update_item("todos", t["id"], {"done": True, "evil": "x", "due": "2026-07-01"})
        got = store.list_items("todos")[0]
        self.assertTrue(got["done"])
        self.assertEqual(got["due"], "2026-07-01")
        self.assertNotIn("evil", got)

    def test_delete_missing_returns_false(self):
        self.assertFalse(store.delete_item("todos", "doesnotexist"))

    # ---- corruption is survivable ----
    def test_corrupt_file_reads_empty(self):
        store._path("todos").write_text("{ this is not json", "utf-8")
        self.assertEqual(store.list_items("todos"), [])

    # ---- settings ----
    def test_settings_roundtrip_and_filter(self):
        s = store.put_settings({"theme": "light", "accent": "#ffff00", "junk": "x"})
        self.assertEqual(s["theme"], "light")
        self.assertEqual(s["accent"], "#ffff00")
        self.assertNotIn("junk", s)

    # ---- ics generation ----
    def test_ics_includes_appointment_and_due_todo(self):
        store.add_item("appointments", {"title": "dr lin", "when": "2026-06-24 14:30"})
        store.add_item("todos", {"title": "buy panel", "due": "2026-06-20"})
        ics = store.build_ics()
        self.assertIn("BEGIN:VCALENDAR", ics)
        self.assertIn("SUMMARY:dr lin", ics)
        self.assertIn("DTSTART:20260624T143000", ics)
        self.assertIn("SUMMARY:todo: buy panel", ics)
        self.assertEqual(ics.count("BEGIN:VEVENT"), 2)

    def test_ics_excludes_done_and_undated_todos(self):
        store.add_item("todos", {"title": "no date"})
        t = store.add_item("todos", {"title": "done one", "due": "2026-06-20"})
        store.update_item("todos", t["id"], {"done": True})
        self.assertEqual(store.build_ics().count("BEGIN:VEVENT"), 0)

    def test_ics_escapes_special_chars(self):
        store.add_item("appointments", {"title": "a; b, c", "when": "2026-06-24"})
        self.assertIn("SUMMARY:a\\; b\\, c", store.build_ics())

    def test_ics_strips_cr_no_injection(self):
        # a bare \r in a title must not forge a CRLF and inject a new property
        store.add_item("appointments", {"title": "x\rINJECTED:true", "when": "2026-06-24"})
        ics = store.build_ics()
        # the CR is gone, so "INJECTED:true" can't start its own property line
        self.assertNotIn("\r\nINJECTED:true", ics)
        self.assertIn("SUMMARY:xINJECTED:true", ics)

    def test_ics_uses_crlf_line_endings(self):
        store.add_item("appointments", {"title": "x", "when": "2026-06-24"})
        ics = store.build_ics()
        self.assertIn("\r\n", ics)
        self.assertNotIn("\r\r", ics)

    # ---- version token changes on write ----
    def test_version_changes_on_write(self):
        v1 = store.version()
        store.add_item("todos", {"title": "x"})
        self.assertNotEqual(v1, store.version())

    # ---- day view ----
    def test_day_groups_by_date(self):
        store.add_item("appointments", {"title": "appt", "when": "2026-06-24 09:00"})
        store.add_item("achievements", {"title": "win", "date": "2026-06-24"})
        d = store.day("2026-06-24")
        self.assertEqual(len(d["appointments"]), 1)
        self.assertEqual(len(d["achievements"]), 1)
        self.assertEqual(len(d["todos"]), 0)

    # ---- recurrence ----
    def test_recur_normalized_and_stored(self):
        a = store.add_item("appointments",
                           {"title": "bhc", "when": "2026-06-11 09:00", "recur": "weekly"})
        self.assertEqual(a["recur"], {"freq": "weekly", "interval": 1, "until": ""})

    def test_recur_bad_freq_dropped(self):
        a = store.add_item("appointments",
                           {"title": "x", "when": "2026-06-11", "recur": {"freq": "yearly"}})
        self.assertEqual(a["recur"], "")

    def test_weekly_biweekly_occurrences(self):
        a = store.add_item("appointments",
                           {"title": "bhc", "when": "2026-06-11 09:00",
                            "recur": {"freq": "weekly", "interval": 2}})
        occ = store.occurrences_in(a, "2026-06-01", "2026-07-31")
        self.assertEqual([w[:10] for w in occ],
                         ["2026-06-11", "2026-06-25", "2026-07-09", "2026-07-23"])
        self.assertTrue(occ[0].endswith("T09:00"))  # time preserved

    def test_occurrence_lands_on_day_view(self):
        store.add_item("appointments",
                       {"title": "bhc", "when": "2026-06-11 09:00",
                        "recur": {"freq": "weekly", "interval": 2}})
        d = store.day("2026-06-25")  # a future occurrence, not the anchor
        self.assertEqual(len(d["appointments"]), 1)
        self.assertEqual(d["appointments"][0]["when"], "2026-06-25T09:00")

    def test_non_occurrence_day_empty(self):
        store.add_item("appointments",
                       {"title": "bhc", "when": "2026-06-11",
                        "recur": {"freq": "weekly", "interval": 2}})
        self.assertEqual(len(store.day("2026-06-18")["appointments"]), 0)  # off-week

    def test_recur_until_caps_series(self):
        a = store.add_item("appointments",
                           {"title": "x", "when": "2026-06-11",
                            "recur": {"freq": "weekly", "interval": 1, "until": "2026-06-25"}})
        occ = [w[:10] for w in store.occurrences_in(a, "2026-06-01", "2026-12-31")]
        self.assertEqual(occ, ["2026-06-11", "2026-06-18", "2026-06-25"])

    def test_next_occurrence(self):
        a = store.add_item("appointments",
                           {"title": "x", "when": "2026-06-11",
                            "recur": {"freq": "weekly", "interval": 2}})
        self.assertEqual(store.next_occurrence(a, "2026-06-12"), "2026-06-25")

    def test_monthly_skips_short_months(self):
        # RRULE semantics: anchor on day 31, skip months without it (no feb)
        a = store.add_item("appointments",
                           {"title": "x", "when": "2026-01-31", "recur": {"freq": "monthly"}})
        occ = [w[:10] for w in store.occurrences_in(a, "2026-01-01", "2026-05-31")]
        self.assertEqual(occ, ["2026-01-31", "2026-03-31", "2026-05-31"])

    def test_monthly_normal_day(self):
        a = store.add_item("appointments",
                           {"title": "x", "when": "2026-01-15", "recur": {"freq": "monthly"}})
        occ = [w[:10] for w in store.occurrences_in(a, "2026-01-01", "2026-03-31")]
        self.assertEqual(occ, ["2026-01-15", "2026-02-15", "2026-03-15"])

    def test_ics_emits_rrule(self):
        store.add_item("appointments",
                       {"title": "bhc", "when": "2026-06-11 09:00",
                        "recur": {"freq": "weekly", "interval": 2}})
        ics = store.build_ics()
        self.assertIn("RRULE:FREQ=WEEKLY;INTERVAL=2", ics)


if __name__ == "__main__":
    unittest.main(verbosity=2)
