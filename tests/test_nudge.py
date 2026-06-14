"""nudge-logic tests — message building, escalation tiers, and the once-per-day /
once-per-week gating. notify.send is stubbed so nothing touches the network.

run:  python3 -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

os.environ["LIFEPLANNER_DATA"] = tempfile.mkdtemp(prefix="lp-nudge-test-")
os.environ["LIFEPLANNER_CALDAV"] = "0"
# configure ntfy + nudge BEFORE import so notify.configured() / the hour consts hold
os.environ["LIFEPLANNER_NTFY_SERVER"] = "http://127.0.0.1:2587"
os.environ["LIFEPLANNER_NTFY_TOPIC"] = "lp-test"
os.environ["LIFEPLANNER_STANDUP_HOUR"] = "8"
os.environ["LIFEPLANNER_REVIEW_HOUR"] = "18"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import notify  # noqa: E402
import nudge  # noqa: E402
import store  # noqa: E402

SUNDAY = datetime(2026, 6, 14, 19, 0)  # weekday set into env below to match
os.environ["LIFEPLANNER_REVIEW_DOW"] = str(SUNDAY.weekday())
nudge.REVIEW_DOW = SUNDAY.weekday()  # const was read at import; align it


def d(offset, base=None):
    return ((base or SUNDAY.date()) + timedelta(days=offset)).isoformat()


class NudgeTest(unittest.TestCase):
    def setUp(self):
        for name in store.ENTITIES + ("settings",):
            p = store._path(name)
            if p.exists():
                p.unlink()
        if nudge.STATE.exists():
            nudge.STATE.unlink()
        if store.LOCK.exists():
            store.LOCK.unlink()
        self.sent = []
        notify.send = lambda *a, **k: self.sent.append((a, k)) or True  # stub network

    # ---- message building ----
    def test_standup_text_lists_overdue(self):
        slip = {"today": d(0), "overdue_todos": [{"title": "taxes", "days_late": 6}],
                "stale_todos": [{"title": "x"}], "days_since_win": 3, "next_load": []}
        txt = nudge.standup_text(slip)
        self.assertIn("1 overdue: taxes 6d", txt)
        self.assertIn("1 stale", txt)
        self.assertIn("3d since a win", txt)

    def test_standup_text_empty_when_clean(self):
        slip = {"today": d(0), "overdue_todos": [], "stale_todos": [],
                "days_since_win": 0, "next_load": []}
        self.assertEqual(nudge.standup_text(slip), "")

    def test_escalation_tiers(self):
        self.assertEqual(nudge._standup_alert([])[0], 3)
        self.assertEqual(nudge._standup_alert([{"days_late": 2}])[0], 3)
        self.assertEqual(nudge._standup_alert([{"days_late": 4}])[0], 4)
        self.assertEqual(nudge._standup_alert([{"days_late": 9}])[0], 5)  # urgent

    def test_review_text(self):
        rv = {"completion_rate": 0.33, "completed_due": 1, "due_in_window": 3,
              "wins_count": 2, "slipped_todos": [{"title": "taxes"}]}
        txt = nudge.review_text(rv)
        self.assertIn("33% done (1/3)", txt)
        self.assertIn("2 wins", txt)
        self.assertIn("1 still open: taxes", txt)

    # ---- gating ----
    def test_standup_fires_once_per_day(self):
        store.add_item("todos", {"title": "taxes", "due": d(-5)})
        nudge.main(now=datetime(2026, 6, 14, 9, 0))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0][0], "standup")
        nudge.main(now=datetime(2026, 6, 14, 10, 0))  # same day again
        self.assertEqual(len(self.sent), 1)  # gated

    def test_standup_not_before_hour(self):
        store.add_item("todos", {"title": "taxes", "due": d(-5)})
        nudge.main(now=datetime(2026, 6, 14, 6, 0))  # before STANDUP_HOUR=8
        self.assertEqual([s for s in self.sent if s[0][0] == "standup"], [])

    def test_clean_day_no_standup_push(self):
        nudge.main(now=datetime(2026, 6, 15, 9, 0))  # nothing seeded, monday
        self.assertEqual([s for s in self.sent if s[0][0] == "standup"], [])

    def test_weekly_review_fires_on_dow(self):
        nudge.main(now=SUNDAY)  # sunday 19:00, review hour passed
        kinds = [s[0][0] for s in self.sent]
        self.assertIn("weekly review", kinds)

    def test_weekly_review_not_other_day(self):
        nudge.main(now=datetime(2026, 6, 15, 19, 0))  # monday
        self.assertNotIn("weekly review", [s[0][0] for s in self.sent])

    def test_disabled_via_env(self):
        os.environ["LIFEPLANNER_NUDGE"] = "off"
        try:
            store.add_item("todos", {"title": "taxes", "due": d(-5)})
            nudge.main(now=datetime(2026, 6, 14, 9, 0))
            self.assertEqual(self.sent, [])
        finally:
            os.environ.pop("LIFEPLANNER_NUDGE")


if __name__ == "__main__":
    unittest.main()
