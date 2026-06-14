"""reminder-logic tests — fire-time math, label formatting, env parsing, and the
state clamp. stdlib unittest only (no ntfy/network: these cover pure functions).

run:  python3 -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

# isolate data + force local backend, and feed a deliberately messy offsets list
# (negative + non-numeric) so we prove the parser drops them — all BEFORE import.
os.environ["LIFEPLANNER_DATA"] = tempfile.mkdtemp(prefix="lp-rem-test-")
os.environ["LIFEPLANNER_CALDAV"] = "0"
os.environ["LIFEPLANNER_REMINDERS"] = "1440,60,-30,abc,120"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reminders  # noqa: E402


class RemindersTest(unittest.TestCase):
    def test_offsets_drops_negative_and_nonnumeric(self):
        # a negative offset would fire *after* the appt and never match the gate —
        # it (and "abc") must be dropped; survivors are sorted descending.
        self.assertEqual(reminders.OFFSETS, [1440, 120, 60])

    def test_label(self):
        self.assertEqual(reminders._label(1440), "in 1 day")
        self.assertEqual(reminders._label(2880), "in 2 days")
        self.assertEqual(reminders._label(60), "in 1 hour")
        self.assertEqual(reminders._label(120), "in 2 hours")
        self.assertEqual(reminders._label(30), "in 30 min")

    def test_fires_timed(self):
        # OFFSETS = [1440, 120, 60]; a timed appt fires once per offset, before it
        fires = list(reminders._fires_for("2026-06-20T14:00"))
        self.assertEqual([f[0] for f in fires], [
            datetime(2026, 6, 19, 14, 0),   # 1 day before
            datetime(2026, 6, 20, 12, 0),   # 2 hours before
            datetime(2026, 6, 20, 13, 0),   # 1 hour before
        ])
        self.assertEqual([f[1] for f in fires], ["in 1 day", "in 2 hours", "in 1 hour"])
        self.assertEqual(len({f[2] for f in fires}), 1)  # one shared when-text

    def test_fires_all_day(self):
        # all-day appts get an evening-before + morning-of nudge, not offset math
        fires = list(reminders._fires_for("2026-06-20"))
        self.assertEqual(fires[0][0], datetime(2026, 6, 19, 18, 0))
        self.assertEqual(fires[0][1], "tomorrow")
        self.assertEqual(fires[1][0], datetime(2026, 6, 20, 8, 0))
        self.assertEqual(fires[1][1], "today")

    def test_load_last_clamps_future(self):
        # a future-dated state (clock jump / hand-edit) must not suppress reminders
        now = datetime(2026, 6, 13, 12, 0)
        reminders.STATE.write_text(datetime(2026, 6, 20, 12, 0).isoformat(), "utf-8")
        try:
            self.assertEqual(reminders._load_last(now), now)
        finally:
            reminders.STATE.unlink()

    def test_load_last_first_run(self):
        # no state yet → start from now so we never replay historical reminders
        now = datetime(2026, 6, 13, 12, 0)
        if reminders.STATE.exists():
            reminders.STATE.unlink()
        self.assertEqual(reminders._load_last(now), now)


if __name__ == "__main__":
    unittest.main()
