"""planning-partner layer tests. stdlib unittest only (no mcp needed).

run:  python3 -m unittest discover -s tests
uses a throwaway data dir and a fixed `today` so every derivation is deterministic.
"""

import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

os.environ["LIFEPLANNER_DATA"] = tempfile.mkdtemp(prefix="lp-test-")
os.environ["LIFEPLANNER_CALDAV"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store  # noqa: E402
import review  # noqa: E402

TODAY = date(2026, 6, 13)


def d(offset):
    return (TODAY + timedelta(days=offset)).isoformat()


class ReviewTest(unittest.TestCase):
    def setUp(self):
        for name in store.ENTITIES + ("settings",):
            p = store._path(name)
            if p.exists():
                p.unlink()
        if store.LOCK.exists():
            store.LOCK.unlink()

    # ---- whats_slipping ----
    def test_overdue_sorted_most_late_first(self):
        store.add_item("todos", {"title": "old", "due": d(-5)})
        store.add_item("todos", {"title": "newer", "due": d(-1)})
        store.add_item("todos", {"title": "future", "due": d(3)})
        out = review.whats_slipping(today=TODAY)
        titles = [t["title"] for t in out["overdue_todos"]]
        self.assertEqual(titles, ["old", "newer"])
        self.assertEqual(out["overdue_todos"][0]["days_late"], 5)

    def test_done_overdue_not_flagged(self):
        t = store.add_item("todos", {"title": "done one", "due": d(-2)})
        store.update_item("todos", t["id"], {"done": True})
        out = review.whats_slipping(today=TODAY)
        self.assertEqual(out["overdue_todos"], [])

    def test_stale_undated_todo(self):
        old = store.add_item("todos", {"title": "languishing"})
        # backdate creation past the staleness cutoff
        items = store.list_items("todos")
        for it in items:
            if it["id"] == old["id"]:
                it["created"] = d(-30)
        store._write_raw("todos", items)
        out = review.whats_slipping(today=TODAY, stale_after=14)
        self.assertEqual(len(out["stale_todos"]), 1)
        self.assertEqual(out["stale_todos"][0]["age_days"], 30)

    def test_dated_todo_never_stale(self):
        store.add_item("todos", {"title": "has a due", "due": d(20)})
        out = review.whats_slipping(today=TODAY)
        self.assertEqual(out["stale_todos"], [])

    def test_win_gap(self):
        store.add_item("achievements", {"title": "w", "date": d(-3)})
        out = review.whats_slipping(today=TODAY)
        self.assertEqual(out["days_since_win"], 3)
        self.assertEqual(out["last_win_date"], d(-3))

    def test_no_wins_yet(self):
        out = review.whats_slipping(today=TODAY)
        self.assertIsNone(out["days_since_win"])
        self.assertIsNone(out["last_win_date"])

    def test_next_load_window(self):
        store.add_item("appointments", {"title": "dentist", "when": d(0) + "T09:00"})
        store.add_item("todos", {"title": "due tomorrow", "due": d(1)})
        store.add_item("appointments", {"title": "later", "when": d(5)})
        out = review.whats_slipping(today=TODAY, horizon=2)
        kinds = sorted(x["kind"] for x in out["next_load"])
        self.assertEqual(kinds, ["appointment", "todo"])
        self.assertTrue(all(x["date"] in (d(0), d(1)) for x in out["next_load"]))

    # ---- review ----
    def test_completion_rate_and_slipped(self):
        a = store.add_item("todos", {"title": "did", "due": d(-2)})
        store.update_item("todos", a["id"], {"done": True})
        store.add_item("todos", {"title": "missed", "due": d(-1)})
        out = review.review(days=7, today=TODAY)
        self.assertEqual(out["due_in_window"], 2)
        self.assertEqual(out["completed_due"], 1)
        self.assertEqual(out["completion_rate"], 0.5)
        self.assertEqual([t["title"] for t in out["slipped_todos"]], ["missed"])

    def test_completion_rate_none_when_nothing_due(self):
        out = review.review(days=7, today=TODAY)
        self.assertIsNone(out["completion_rate"])

    def test_completed_includes_undated(self):
        a = store.add_item("todos", {"title": "no due, done"})
        store.update_item("todos", a["id"], {"done": True})
        # done_at is stamped off the real clock; pin it in-window deterministically
        items = store.list_items("todos")
        for it in items:
            if it["id"] == a["id"]:
                it["done_at"] = d(-1)
        store._write_raw("todos", items)
        out = review.review(days=7, today=TODAY)
        self.assertEqual(len(out["completed_todos"]), 1)
        self.assertEqual(out["due_in_window"], 0)  # not counted in the rate

    def test_wins_in_window_only(self):
        store.add_item("achievements", {"title": "recent", "date": d(-3)})
        store.add_item("achievements", {"title": "ancient", "date": d(-40)})
        out = review.review(days=7, today=TODAY)
        self.assertEqual(out["wins_count"], 1)
        self.assertEqual(out["wins"][0]["title"], "recent")

    def test_busiest_day(self):
        store.add_item("appointments", {"title": "a1", "when": d(-1)})
        store.add_item("appointments", {"title": "a2", "when": d(-1)})
        store.add_item("achievements", {"title": "w", "date": d(-2)})
        out = review.review(days=7, today=TODAY)
        self.assertEqual(out["busiest_day"]["date"], d(-1))
        self.assertEqual(out["busiest_day"]["items"], 2)

    def test_days_clamped(self):
        self.assertEqual(review.review(days=999, today=TODAY)["days"], 60)
        self.assertEqual(review.review(days=0, today=TODAY)["days"], 1)


if __name__ == "__main__":
    unittest.main()
