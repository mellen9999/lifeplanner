"""directive tests — the coach's silence rules.

the coach's failure mode was noise: the same nag rewritten every two hours,
grounded in a memory note from twelve days ago, with no way to kill it. these
lock the three mechanisms that fixed it — the fingerprint gate, the anti-repeat
guarantee, and the dismissal lifecycle. the claude subprocess is never invoked.

run:  python3 -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["LIFEPLANNER_DATA"] = tempfile.mkdtemp(prefix="lp-brief-test-")
os.environ["LIFEPLANNER_CALDAV"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import brief  # noqa: E402
import store  # noqa: E402


class DirectiveLifecycleTest(unittest.TestCase):
    def setUp(self):
        for name in ("coach", "coach_memory"):
            p = store._path(name)
            if p.exists():
                p.unlink()

    def test_a_new_line_resets_age_and_records_its_state(self):
        c = store.set_coach("ship the auth flow", "fp1")
        self.assertEqual(c["line"], "ship the auth flow")
        self.assertEqual(c["fp"], "fp1")
        self.assertTrue(c["created"])

    def test_the_same_line_never_flips_the_age(self):
        first = store.set_coach("ship the auth flow", "fp1")
        again = store.set_coach("ship the auth flow", "fp2")
        self.assertEqual(again["created"], first["created"])  # a re-say is not news

    def test_unchanged_state_is_current_and_a_moved_state_is_not(self):
        store.set_coach("ship the auth flow", "fp1")
        self.assertTrue(store.coach_is_current("fp1"))
        self.assertFalse(store.coach_is_current("fp2"))
        self.assertFalse(store.coach_is_current(""))

    def test_a_line_with_no_fingerprint_is_stale(self):
        # pre-lifecycle data must be re-judged once, never trusted as current.
        store._write_raw("coach", {"line": "old line", "created": "2026-08-01T09:00"})
        self.assertFalse(store.coach_is_current("fp1"))

    def test_touch_keeps_the_line_and_only_marks_the_state_judged(self):
        store.set_coach("ship the auth flow", "fp1")
        store.dismiss_coach()
        store.touch_coach("fp2")
        c = store.get_coach()
        self.assertEqual(c["line"], "ship the auth flow")
        self.assertTrue(c["dismissed"])       # "nothing new" must not revive it
        self.assertTrue(store.coach_is_current("fp2"))

    def test_dismissal_hides_the_line_until_a_new_one_is_written(self):
        store.set_coach("ship the auth flow", "fp1")
        self.assertTrue(store.dismiss_coach())
        self.assertFalse(store.dismiss_coach())          # already dead
        self.assertEqual(store.state()["coach"]["line"], "")
        store.set_coach("call the landlord", "fp2")
        self.assertEqual(store.state()["coach"]["line"], "call the landlord")

    def test_the_writer_is_told_which_lines_were_rejected(self):
        store.set_coach("ship the auth flow", "fp1")
        store.dismiss_coach()
        store.set_coach("call the landlord", "fp2")
        recent = store.coach_recent_lines()
        self.assertEqual(recent[-1], {"line": "ship the auth flow", "dismissed": True})
        self.assertIn("[dismissed]", brief._recent_block())

    def test_recent_lines_stay_bounded(self):
        for i in range(store.COACH_RECENT_MAX + 4):
            store.set_coach(f"line {i}", f"fp{i}")
        self.assertEqual(len(store.coach_recent_lines()), store.COACH_RECENT_MAX)

    def test_a_repeat_is_rejected_in_code_not_left_to_the_model(self):
        store.set_coach("ship the auth flow", "fp1")
        store.set_coach("call the landlord", "fp2")
        brief.subprocess.run = lambda *a, **k: _Run("Ship the auth flow.")
        self.assertEqual(brief.ask("2026-08-18", "state"), "-")   # said before
        brief.subprocess.run = lambda *a, **k: _Run("call the landlord")
        self.assertEqual(brief.ask("2026-08-18", "state"), "-")   # the live line
        brief.subprocess.run = lambda *a, **k: _Run("book the dentist")
        self.assertEqual(brief.ask("2026-08-18", "state"), "book the dentist")

    def test_junk_output_keeps_the_previous_line(self):
        for bad in ("", "API Error: overloaded", "i can't help with that", "x" * 300):
            brief.subprocess.run = lambda *a, **k: _Run(bad)
            self.assertIsNone(brief.ask("2026-08-18", "state"))

    def test_a_dash_means_silence(self):
        brief.subprocess.run = lambda *a, **k: _Run("`-`")
        self.assertEqual(brief.ask("2026-08-18", "state"), "-")

    def test_claude_being_unavailable_never_raises(self):
        brief.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
        self.assertIsNone(brief.ask("2026-08-18", "state"))

    def test_the_fingerprint_moves_only_when_the_state_does(self):
        self.assertEqual(brief.fingerprint("a\nb"), brief.fingerprint("a\nb"))
        self.assertNotEqual(brief.fingerprint("a\nb"), brief.fingerprint("a\nc"))

    def test_a_line_is_pushed_once_and_a_dismissed_one_never_is(self):
        store.set_coach("ship the auth flow", "fp1")
        self.assertTrue(store.mark_coach_pushed())
        self.assertFalse(store.mark_coach_pushed())       # never twice
        store.set_coach("call the landlord", "fp2")
        store.dismiss_coach()
        self.assertFalse(store.mark_coach_pushed())       # rejected, so unsent


class _Run:
    """stand-in for a finished subprocess run."""

    def __init__(self, out):
        self.stdout, self.stderr, self.returncode = out, "", 0


if __name__ == "__main__":
    unittest.main()
