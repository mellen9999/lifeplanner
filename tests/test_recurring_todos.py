"""recurring todos (daily/weekly routines) — recurrence expansion, per-date
completion, the one-off vs routine split, and .ics output. stdlib unittest only.

run:  python3 -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

os.environ["LIFEPLANNER_DATA"] = tempfile.mkdtemp(prefix="lp-rt-test-")
os.environ["LIFEPLANNER_CALDAV"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store  # noqa: E402

T = date(2026, 6, 14)


def d(n):
    return (T + timedelta(days=n)).isoformat()


class RecurringTodoTest(unittest.TestCase):
    def setUp(self):
        for name in store.ENTITIES + ("settings",):
            p = store._path(name)
            if p.exists():
                p.unlink()
        if store.LOCK.exists():
            store.LOCK.unlink()

    def _daily(self, title="workout", anchor=None):
        return store.add_item("todos", {"title": title, "due": anchor or d(0),
                                        "recur": "daily"})

    # ---- shape / normalization ----
    def test_recurring_todo_shape(self):
        t = self._daily()
        self.assertEqual(t["recur"], {"freq": "daily", "interval": 1, "until": ""})
        self.assertEqual(t["done_dates"], [])
        self.assertFalse(t["done"])

    def test_done_dates_validated_on_add(self):
        t = store.add_item("todos", {"title": "x", "due": d(0), "recur": "daily",
                                     "done_dates": [d(0), "garbage", d(1), d(0)]})
        self.assertEqual(t["done_dates"], sorted([d(0), d(1)]))  # deduped, junk dropped

    # ---- occurrence expansion ----
    def test_occurrences_daily(self):
        t = self._daily(anchor=d(0))
        occ = store.todo_occurrences(t, d(0), d(3))
        self.assertEqual(occ, [d(0), d(1), d(2), d(3)])

    def test_days_expands_recurring_with_done_state(self):
        t = self._daily(anchor=d(0))
        store.set_todo_done(t["id"], d(1), True)
        grid = store.days(d(0), d(2))
        # each day carries the routine, tagged with that day's completion
        self.assertFalse(grid[d(0)]["todos"][0]["done"])
        self.assertTrue(grid[d(1)]["todos"][0]["done"])
        self.assertEqual(grid[d(1)]["todos"][0]["due"], d(1))

    # ---- per-date completion ----
    def test_set_done_recurring_per_date(self):
        t = self._daily()
        store.set_todo_done(t["id"], d(0), True)
        store.set_todo_done(t["id"], d(2), True)
        cur = next(x for x in store.list_items("todos") if x["id"] == t["id"])
        self.assertEqual(cur["done_dates"], sorted([d(0), d(2)]))
        self.assertFalse(cur["done"])  # global flag never set for a routine
        self.assertTrue(store.todo_done_on(cur, d(0)))
        self.assertFalse(store.todo_done_on(cur, d(1)))

    def test_uncomplete_recurring(self):
        t = self._daily()
        store.set_todo_done(t["id"], d(0), True)
        store.set_todo_done(t["id"], d(0), False)
        cur = next(x for x in store.list_items("todos") if x["id"] == t["id"])
        self.assertEqual(cur["done_dates"], [])

    def test_set_done_oneoff_uses_flag(self):
        t = store.add_item("todos", {"title": "taxes", "due": d(0)})
        store.set_todo_done(t["id"], "", True)
        cur = next(x for x in store.list_items("todos") if x["id"] == t["id"])
        self.assertTrue(cur["done"])
        self.assertEqual(cur["done_at"], date.today().isoformat())
        self.assertEqual(cur["done_dates"], [])

    # ---- editing across the one-off / routine boundary ----
    def test_oneoff_to_recurring_clears_flag(self):
        t = store.add_item("todos", {"title": "x", "due": d(0)})
        store.update_item("todos", t["id"], {"done": True})
        store.update_item("todos", t["id"], {"recur": "daily"})
        cur = next(x for x in store.list_items("todos") if x["id"] == t["id"])
        self.assertFalse(cur["done"])
        self.assertEqual(cur["done_at"], "")

    def test_done_patch_ignored_on_routine(self):
        t = self._daily()
        store.update_item("todos", t["id"], {"done": True})  # must not stick
        cur = next(x for x in store.list_items("todos") if x["id"] == t["id"])
        self.assertFalse(cur["done"])

    # ---- ics ----
    def test_ics_recurring_todo_has_rrule(self):
        self._daily(title="meds")
        ics = store.build_ics()
        self.assertIn("todo: meds", ics)
        self.assertIn("RRULE:FREQ=DAILY", ics)

    def test_ics_oneoff_done_skipped_recurring_kept(self):
        one = store.add_item("todos", {"title": "oneoff", "due": d(0)})
        store.set_todo_done(one["id"], "", True)
        self._daily(title="routine")
        ics = store.build_ics()
        self.assertNotIn("todo: oneoff", ics)   # done one-off dropped
        self.assertIn("todo: routine", ics)      # routine always shown


class RoutineReportTest(unittest.TestCase):
    def setUp(self):
        for name in store.ENTITIES + ("settings",):
            p = store._path(name)
            if p.exists():
                p.unlink()
        if store.LOCK.exists():
            store.LOCK.unlink()

    def test_review_routine_consistency(self):
        import review
        w = store.add_item("todos", {"title": "workout", "due": d(-6), "recur": "daily"})
        for n in (-6, -4, -2):  # done 3 of the 7 days in the window
            store.set_todo_done(w["id"], d(n), True)
        store.add_item("todos", {"title": "meds", "due": d(-6), "recur": "daily"})  # 0/7
        rv = review.review(days=7, today=T)
        rs = {r["title"]: (r["done"], r["total"]) for r in rv["routines"]}
        self.assertEqual(rs["workout"], (3, 7))
        self.assertEqual(rs["meds"], (0, 7))
        self.assertEqual(rv["routine_total"], 14)
        self.assertEqual(rv["routine_completions"], 3)
        # sorted worst-first → meds (0/7) before workout (3/7)
        self.assertEqual(rv["routines"][0]["title"], "meds")

    def test_whats_slipping_routines_today(self):
        import review
        store.add_item("todos", {"title": "workout", "due": T.isoformat(), "recur": "daily"})
        done = store.add_item("todos", {"title": "meds", "due": T.isoformat(), "recur": "daily"})
        store.set_todo_done(done["id"], T.isoformat(), True)
        out = review.whats_slipping(today=T)
        titles = [r["title"] for r in out["routines_today"]]
        self.assertEqual(titles, ["workout"])  # meds already ticked today → not listed


class StatsTest(unittest.TestCase):
    def setUp(self):
        for name in store.ENTITIES + ("settings",):
            p = store._path(name)
            if p.exists():
                p.unlink()
        if store.LOCK.exists():
            store.LOCK.unlink()

    def test_stats_aggregates_any_range(self):
        import review
        store.add_item("achievements", {"title": "a", "date": "2024-03-05"})
        store.add_item("achievements", {"title": "b", "date": "2024-03-20"})
        store.add_item("achievements", {"title": "c", "date": "2024-08-01"})
        store.add_item("achievements", {"title": "d", "date": "2025-01-01"})  # out of range
        out = review.stats("2024-01-01", "2024-12-31")
        self.assertEqual(out["wins"], 3)
        self.assertEqual(out["active_days"], 3)
        self.assertEqual(out["wins_by_month"], {"2024-03": 2, "2024-08": 1})

    def test_stats_bad_dates(self):
        import review
        self.assertIn("error", review.stats("nope", "2024-01-01"))


if __name__ == "__main__":
    unittest.main()
