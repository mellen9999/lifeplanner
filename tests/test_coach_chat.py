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
import store  # noqa: E402


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
        self.assertEqual(coach_chat.respond("  "), {"error": "empty message"})

    def test_transcript_labels_and_orders_turns(self):
        history = [{"role": "you", "text": "hi"}, {"role": "coach", "text": "hey"}]
        t = coach_chat._transcript(history, "what's next")
        self.assertEqual(
            t.splitlines(),
            ["mellen: hi", "coach: hey", "mellen: what's next", "coach:"])

    def test_transcript_skips_blank_and_nondict_turns(self):
        t = coach_chat._transcript([{"role": "you", "text": ""}, "junk", None], "go")
        self.assertEqual(t.splitlines(), ["mellen: go", "coach:"])


class CoachMemoryTest(unittest.TestCase):
    """the coach's persistence: chat turns and remembered facts survive in the
    store, junk never does, and a failed claude run still keeps mellen's words."""

    def setUp(self):
        store._write_raw("coach_chat", [])
        store._write_raw("coach_memory", [])

    def test_chat_turns_persist_in_order(self):
        store.log_coach_turn("you", "first")
        store.log_coach_turn("coach", "second")
        tail = store.coach_chat_tail()
        self.assertEqual([(t["role"], t["text"]) for t in tail],
                         [("you", "first"), ("coach", "second")])
        self.assertTrue(all(t.get("ts") for t in tail))

    def test_blank_or_bad_role_turns_are_dropped(self):
        self.assertIsNone(store.log_coach_turn("you", "   "))
        self.assertIsNone(store.log_coach_turn("hacker", "hi"))
        self.assertEqual(store.coach_chat_tail(), [])

    def test_chat_tail_caps_and_turn_text_is_capped(self):
        for i in range(20):
            store.log_coach_turn("you", f"msg {i}")
        self.assertEqual(len(store.coach_chat_tail(12)), 12)
        store.log_coach_turn("you", "x" * 99999)
        self.assertEqual(len(store.coach_chat_tail(1)[0]["text"]), store.COACH_MSG_MAX)

    def test_memory_notes_are_normalized_and_capped(self):
        store.add_coach_memory("  hates   phone\n\nnotifications  ")
        notes = store.list_coach_memory()
        self.assertEqual(notes[0]["note"], "hates phone notifications")
        self.assertTrue(notes[0]["date"])
        self.assertIsNone(store.add_coach_memory("   "))
        store.add_coach_memory("y" * 9999)
        self.assertEqual(len(store.list_coach_memory()[-1]["note"]), store.COACH_NOTE_MAX)

    def test_state_carries_the_chat_tail(self):
        store.log_coach_turn("you", "hello")
        self.assertEqual(store.state()["coach"]["chat"][-1]["text"], "hello")

    def test_memory_summary_inlines_recent_and_points_at_the_rest(self):
        self.assertEqual(coach_chat._memory_summary(), "nothing saved yet")
        for i in range(coach_chat.MAX_NOTES + 5):
            store.add_coach_memory(f"fact {i}")
        s = coach_chat._memory_summary()
        self.assertIn(f"fact {coach_chat.MAX_NOTES + 4}", s)   # newest inlined
        self.assertNotIn(": fact 0\n", s)                      # oldest not
        self.assertIn("5 older notes", s)                      # and flagged

    def test_failed_claude_run_still_keeps_the_user_turn(self):
        # "remember all of it": even a coach crash never loses what mellen typed.
        real = coach_chat.subprocess.run
        coach_chat.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
        try:
            r = coach_chat.respond("important brain dump")
        finally:
            coach_chat.subprocess.run = real
        self.assertIn("error", r)
        self.assertEqual(store.coach_chat_tail(1)[0]["text"], "important brain dump")

    def test_memory_tools_are_whitelisted(self):
        for t in ("remember", "list_memory"):
            self.assertIn(f"mcp__lifeplanner__{t}", coach_chat.ALLOWED)


if __name__ == "__main__":
    unittest.main()
