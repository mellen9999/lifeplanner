#!/usr/bin/env python3
"""distill — nightly memory backstop for the coach chat.

the live coach is ASKED to call remember when mellen tells it something durable,
but that's at claude's discretion. this makes it deterministic: every night the
day's new chat turns are re-read and any durable facts claude missed are written
into coach memory. exactly-once via a cursor into the append-only transcript
(bumped only on success, so a failed night is retried and the store's exact-dup
guard absorbs the overlap). pure python + subprocess with input= — stdin is owned
by nothing else, so the heredoc-eats-stdin bug class can't recur here.

runs on lifeplanner-distill.timer at 22:50 (after the diary, before the digest).
--dry-run prints what would be written and touches nothing.

what it must NOT save is the point: memory is re-read into every coach prompt
forever, so a transient status ("hasn't done x yet") saved as a durable fact is
how a twelve-day-old situation comes back as today's advice.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store  # noqa: E402

CLAUDE = os.environ.get("LIFEPLANNER_CLAUDE_BIN") or "claude"
DRY = "--dry-run" in sys.argv
MAX_FACTS = 8       # runaway guard — a night yields a few facts, never a flood
TIMEOUT = 180
REJECT = re.compile(r"api error|rate.?limit|usage limit|error:|i can'?t|i cannot", re.I)


def main():
    raw = store._read_raw("coach_chat", [])
    turns = [t for t in raw if isinstance(t, dict)] if isinstance(raw, list) else []
    cursor = store.coach_distill_cursor()
    new_cursor = len(turns)  # captured BEFORE claude runs — turns landing mid-run
    # are next night's work, never skipped and never double-read
    fresh = turns[cursor:new_cursor]
    if not any(t.get("role") == "you" for t in fresh):
        if not DRY:
            store.set_coach_distill_cursor(new_cursor)
        print(f"nothing new to distill (cursor {cursor} -> {new_cursor})")
        return

    convo = "\n".join(
        f'{"mellen" if t.get("role") == "you" else "coach"}: {t.get("text", "")[:4000]}'
        for t in fresh if t.get("role") in ("you", "coach") and t.get("text"))
    notes, _ = store.coach_memory_window()
    known = "\n".join(f'- {n["note"]}' for n in notes) or "(nothing saved yet)"

    prompt = f"""you are the memory distiller for mellen's lifeplanner coach.
below is what the coach already knows, then today's new chat turns.

already saved:
{known}

new conversation:
{convo}

extract facts that will still be true and still be worth knowing in six months —
preferences, constraints, how he works, standing situations, long-running plans.

save NOTHING transient. no progress reports, no "hasn't done x yet", no "last
win was n days ago", no "is waiting on y", no one-off scheduling details, no
small talk, nothing already covered above. these notes are re-read into every
future coach prompt, so a snapshot of today comes back as advice for weeks —
if it would read as wrong or stale in a month, leave it out.

one fact per line, lowercase, present tense, each under 400 chars.
the first line of your output must be exactly: facts
if there is nothing durable and new, output exactly: none
output nothing else."""

    try:
        r = subprocess.run(["claude", "-p"], input=prompt, capture_output=True,
                           text=True, timeout=TIMEOUT, cwd="/tmp")
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"claude unavailable ({e}) — cursor untouched, retried tomorrow")
        return
    out = (r.stdout or "").strip()
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    if not lines or REJECT.search(out):
        print("claude output rejected — cursor untouched, retried tomorrow")
        return
    head, facts = lines[0], lines[1:]
    if head == "none":
        if not DRY:
            store.set_coach_distill_cursor(new_cursor)
        print(f"nothing durable in {len(fresh)} turns (cursor -> {new_cursor})")
        return
    if head != "facts":
        print("claude output rejected (bad sentinel) — cursor untouched")
        return

    facts = [f[:400] for f in facts[:MAX_FACTS]]
    if DRY:
        print(f"dry run — would save {len(facts)} fact(s), cursor -> {new_cursor}:")
        for f in facts:
            print(f"  {f}")
        return
    saved = sum(1 for f in facts if store.add_coach_memory(f))
    store.set_coach_distill_cursor(new_cursor)
    print(f"distilled {len(fresh)} turns -> {saved} fact(s) (cursor -> {new_cursor})")


if __name__ == "__main__":
    main()
