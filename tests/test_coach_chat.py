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

    def test_transcript_spells_out_a_failed_turn(self):
        # a system marker must never read as a normal coach reply — the model is
        # told explicitly the message above went unanswered.
        history = [{"role": "you", "text": "hi"},
                   {"role": "system", "text": "coach timed out — try again"}]
        t = coach_chat._transcript(history, "still there?")
        self.assertEqual(t.splitlines()[1],
                         "coach: (no reply — coach timed out — try again; the message above went unanswered)")

    def test_transcript_keeps_the_live_message_untrimmed(self):
        # history turns are trimmed to bound the prompt; the message being
        # answered is not — "brain-dump whole paragraphs" must hold.
        long = "z" * (coach_chat.MAX_MSG + 500)
        t = coach_chat._transcript([{"role": "you", "text": long}], long)
        lines = t.splitlines()
        self.assertEqual(len(lines[0]), len("mellen: ") + coach_chat.MAX_MSG)  # history: trimmed
        self.assertEqual(len(lines[1]), len("mellen: ") + len(long))           # live: intact

    def test_forget_is_not_allowed_but_chat_reading_is(self):
        self.assertNotIn("mcp__lifeplanner__forget", coach_chat.ALLOWED)
        self.assertIn("mcp__lifeplanner__get_coach_chat", coach_chat.ALLOWED)


class CoachMemoryTest(unittest.TestCase):
    """the coach's persistence: chat turns and remembered facts survive in the
    store, junk never does, and a failed claude run still keeps mellen's words."""

    def setUp(self):
        store._write_raw("coach_chat", [])
        store._write_raw("coach_memory", [])
        store._write_raw("coach_distill", {})

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
        self.assertEqual(store.log_coach_turn("system", "no reply")["role"], "system")

    def test_long_turns_persist_in_full_up_to_the_bound(self):
        # 4000 was a PROMPT window, never a persistence cap — a real brain dump
        # survives whole. only the runaway 64k bound cuts, and it says so.
        store.log_coach_turn("you", "x" * 50_000)
        t = store.coach_chat_tail(1)[0]
        self.assertEqual(len(t["text"]), 50_000)
        self.assertNotIn("clipped", t)
        turn = store.log_coach_turn("you", "x" * 70_000)
        self.assertEqual(len(turn["text"]), store.COACH_TURN_MAX)
        self.assertTrue(turn["clipped"])

    def test_chat_tail_caps(self):
        for i in range(20):
            store.log_coach_turn("you", f"msg {i}")
        self.assertEqual(len(store.coach_chat_tail(12)), 12)

    def test_chat_page_paginates_on_stable_absolute_indices(self):
        for i in range(10):
            store.log_coach_turn("you", f"msg {i}")
        page = store.coach_chat_page(limit=3)
        self.assertEqual(page["total"], 10)
        self.assertEqual([t["i"] for t in page["turns"]], [7, 8, 9])
        older = store.coach_chat_page(limit=3, before_index=7)
        self.assertEqual([t["text"] for t in older["turns"]], ["msg 4", "msg 5", "msg 6"])
        self.assertEqual(store.coach_chat_page(limit=99999)["total"], 10)  # clamp doesn't crash
        self.assertEqual(store.coach_chat_page(limit=5, before_index=0)["turns"], [])

    def test_memory_notes_are_normalized_and_capped(self):
        store.add_coach_memory("  hates   phone\n\nnotifications  ")
        notes = store.list_coach_memory()
        self.assertEqual(notes[0]["note"], "hates phone notifications")
        self.assertTrue(notes[0]["date"])
        self.assertIsNone(store.add_coach_memory("   "))
        store.add_coach_memory("y" * 9999)
        self.assertEqual(len(store.list_coach_memory()[-1]["note"]), store.COACH_NOTE_MAX)

    def test_memory_notes_get_ids_and_dupes_return_the_existing(self):
        a = store.add_coach_memory("pc first, phone is friction")
        self.assertTrue(a["id"])
        b = store.add_coach_memory("  pc  first,  phone is friction ")  # normalizes to same
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(store.list_coach_memory()), 1)

    def test_idless_notes_are_migrated_once_with_stable_ids(self):
        # the pre-id era: raw notes on disk gain ids on first read, then keep them.
        store._write_raw("coach_memory", [{"note": "old fact", "date": "2026-08-01"}])
        first = store.list_coach_memory()
        self.assertTrue(first[0]["id"])
        self.assertEqual(store.list_coach_memory()[0]["id"], first[0]["id"])

    def test_delete_memory_hits_and_misses(self):
        e = store.add_coach_memory("wrong fact")
        self.assertTrue(store.delete_coach_memory(e["id"]))
        self.assertEqual(store.list_coach_memory(), [])
        self.assertFalse(store.delete_coach_memory(e["id"]))
        self.assertFalse(store.delete_coach_memory(""))

    def test_memory_window_keeps_newest_under_budget(self):
        for i in range(30):
            store.add_coach_memory(f"fact {i:02d} " + "p" * 400)
        kept, omitted = store.coach_memory_window(budget=2000)
        self.assertEqual(omitted, 30 - len(kept))
        self.assertEqual(kept[-1]["note"][:7], "fact 29")            # newest kept
        self.assertLess(len(kept), 30)                               # something omitted
        self.assertEqual([n["note"][:7] for n in kept],
                         sorted(n["note"][:7] for n in kept))        # oldest-first render

    def test_distill_cursor_round_trips(self):
        self.assertEqual(store.coach_distill_cursor(), 0)
        store.set_coach_distill_cursor(7)
        self.assertEqual(store.coach_distill_cursor(), 7)
        store._write_raw("coach_distill", {"cursor": "junk"})
        self.assertEqual(store.coach_distill_cursor(), 0)

    def test_state_carries_the_chat_tail(self):
        store.log_coach_turn("you", "hello")
        self.assertEqual(store.state()["coach"]["chat"][-1]["text"], "hello")

    def test_memory_summary_inlines_the_window_and_points_at_the_rest(self):
        self.assertEqual(coach_chat._memory_summary(), "nothing saved yet")
        # enough max-length notes to overflow the shared budget
        n = store.COACH_MEM_BUDGET // store.COACH_NOTE_MAX + 5
        for i in range(n):
            store.add_coach_memory(f"fact {i:03d} " + "p" * 480)
        s = coach_chat._memory_summary()
        self.assertIn(f"fact {n - 1:03d}", s)      # newest inlined
        self.assertNotIn("fact 000", s)            # oldest not
        self.assertIn("older notes", s)            # and flagged

    def test_failed_claude_run_keeps_the_turn_and_leaves_a_marker(self):
        # "remember all of it": even a coach crash never loses what mellen typed —
        # and the transcript says the turn failed instead of dangling unanswered.
        real = coach_chat.subprocess.run
        coach_chat.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
        try:
            r = coach_chat.respond("important brain dump " * 1000)  # ~21k chars
        finally:
            coach_chat.subprocess.run = real
        self.assertIn("error", r)
        tail = store.coach_chat_tail(2)
        self.assertEqual(tail[0]["role"], "you")
        self.assertEqual(len(tail[0]["text"]), len(("important brain dump " * 1000).strip()))
        self.assertEqual(tail[1]["role"], "system")

    def test_memory_tools_are_whitelisted(self):
        for t in ("remember", "list_memory"):
            self.assertIn(f"mcp__lifeplanner__{t}", coach_chat.ALLOWED)


if __name__ == "__main__":
    unittest.main()
