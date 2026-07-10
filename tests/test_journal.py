"""diary/journal entity tests. stdlib unittest only.

the journal is the odd one out among the entities: no title (its required field is
`body`), and its `when` carries a time-of-day that must survive a blank/backdated
save. these lock that behaviour in — and that a private diary never leaks into the
shared .ics feed.

run:  python3 -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

# isolate the data dir BEFORE importing store (store reads the env at import time)
os.environ["LIFEPLANNER_DATA"] = tempfile.mkdtemp(prefix="lp-test-journal-")
os.environ["LIFEPLANNER_CALDAV"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store  # noqa: E402


class JournalTest(unittest.TestCase):
    def setUp(self):
        for name in store.ENTITIES + ("settings",):
            p = store._path(name)
            if p.exists():
                p.unlink()
        if store.LOCK.exists():
            store.LOCK.unlink()

    # ---- required field is body, not title ----
    def test_body_required_no_title(self):
        j = store.add_item("journal", {"body": "wrote something"})
        self.assertEqual(j["body"], "wrote something")
        self.assertNotIn("title", j)  # a diary entry has no headline
        with self.assertRaises(ValueError):
            store.add_item("journal", {"body": "   "})

    # ---- when defaults to now WITH a time (not a bare date) ----
    def test_blank_when_stamps_now_with_time(self):
        j = store.add_item("journal", {"body": "logged now"})
        self.assertGreater(len(j["when"]), 10)  # has a THH:MM, not just a date
        self.assertEqual(j["when"][:10], date.today().isoformat())

    # ---- a memory can be backdated, keeping any time given ----
    def test_backdate_preserves_moment(self):
        j = store.add_item("journal", {"body": "a trip", "when": "2019-08-15 14:22"})
        self.assertEqual(j["when"], "2019-08-15T14:22")
        d = store.add_item("journal", {"body": "day memory", "when": "2019-08-15"})
        self.assertEqual(d["when"], "2019-08-15")  # day-level memory keeps no time

    # ---- day/range views bucket journal by its when's date ----
    def test_days_buckets_journal(self):
        store.add_item("journal", {"body": "x", "when": "2026-07-09 09:00"})
        store.add_item("journal", {"body": "y", "when": "2026-07-09 21:00"})
        d = store.day("2026-07-09")
        self.assertEqual(len(d["journal"]), 2)
        self.assertIn("journal", store.day("2026-07-10"))  # empty shape still has the key

    # ---- state returns entries newest-first ----
    def test_state_newest_first(self):
        store.add_item("journal", {"body": "older", "when": "2026-07-01 10:00"})
        store.add_item("journal", {"body": "newer", "when": "2026-07-08 10:00"})
        bodies = [j["body"] for j in store.state()["journal"]]
        self.assertEqual(bodies, ["newer", "older"])

    # ---- a private diary must never appear in the shared calendar feed ----
    def test_never_in_ics(self):
        store.add_item("journal", {"body": "private thought", "when": "2026-07-09 10:00"})
        self.assertNotIn("private thought", store.build_ics())

    # ---- editing body + re-dating works through the generic update path ----
    def test_update(self):
        j = store.add_item("journal", {"body": "draft"})
        u = store.update_item("journal", j["id"], {"body": "final", "when": "2020-01-01"})
        self.assertEqual(u["body"], "final")
        self.assertEqual(u["when"], "2020-01-01")

    # ---- a journal entry round-trips through the backup zip ----
    def test_in_export(self):
        store.add_item("journal", {"body": "keep me"})
        import io
        import zipfile
        z = zipfile.ZipFile(io.BytesIO(store.export_bytes()))
        self.assertIn("journal.json", z.namelist())


if __name__ == "__main__":
    unittest.main()
