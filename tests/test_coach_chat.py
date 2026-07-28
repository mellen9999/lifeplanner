"""coach-chat containment tests — the agentic coach can act on the planner but
must NEVER be able to delete data or touch the shell/filesystem. these lock the
tool scope so a future edit can't silently widen it. stdlib unittest only; the
claude subprocess is never invoked (we test the pure scoping + prompt build).

run:  python3 -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["LIFEPLANNER_DATA"] = tempfile.mkdtemp(prefix="lp-coach-test-")
os.environ["LIFEPLANNER_CALDAV"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coach_chat  # noqa: E402


class CoachContainmentTest(unittest.TestCase):
    def test_no_destructive_tool_is_allowed(self):
        # the single most important invariant: the coach cannot delete anything.
        for name in coach_chat.ALLOWED:
            self.assertNotIn("delete", name.lower())

    def test_shell_and_file_tools_are_denied(self):
        for t in ("Bash", "Read", "Write", "Edit"):
            self.assertIn(t, coach_chat.DISALLOWED)

    def test_allowed_tools_are_all_lifeplanner_scoped(self):
        # nothing outside the app's own MCP server may be whitelisted.
        for name in coach_chat.ALLOWED:
            self.assertTrue(name.startswith("mcp__lifeplanner__"), name)

    def test_empty_message_is_rejected_without_shelling_out(self):
        # guards the fast-path: a blank message never spawns claude.
        self.assertEqual(coach_chat.respond("  ", []), {"error": "empty message"})

    def test_transcript_labels_and_orders_turns(self):
        history = [{"role": "you", "text": "hi"}, {"role": "coach", "text": "hey"}]
        t = coach_chat._transcript(history, "what's next")
        self.assertEqual(
            t.splitlines(),
            ["mellen: hi", "coach: hey", "mellen: what's next", "coach:"])

    def test_transcript_skips_blank_and_nondict_turns(self):
        t = coach_chat._transcript([{"role": "you", "text": ""}, "junk", None], "go")
        self.assertEqual(t.splitlines(), ["mellen: go", "coach:"])


if __name__ == "__main__":
    unittest.main()
