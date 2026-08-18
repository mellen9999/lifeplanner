#!/usr/bin/env python3
"""brief — the coach directive: ONE line naming the next optimal move.

runs on a timer but behaves like a service, not a broadcaster. three rules keep
it quiet: it only speaks when the state it last judged has actually changed (a
fingerprint gate, so an unchanged planner costs zero tokens and zero noise), it
is shown its own recent lines and which of them mellen dismissed so it can never
re-nag, and a repeat is rejected here in code rather than trusted to the model.
it may also answer "-" for "nothing new to say". every failure path leaves the
previous line exactly as it was.

  brief.py            write the directive if the state moved
  brief.py --notify   ...and push it once via ntfy if it hasn't gone out yet
  brief.py --force    ask again even when the state is unchanged
  brief.py --dry-run  print what it would do, write nothing

mele-local (the store + the claude cli). driven by lifeplanner-brief.timer.
"""

import hashlib
import os
import re
import subprocess
import sys
from datetime import date

import coach_chat
import notify
import store

CLAUDE = os.environ.get("LIFEPLANNER_CLAUDE_BIN") or "claude"
TIMEOUT = int(os.environ.get("LIFEPLANNER_BRIEF_TIMEOUT", "180"))
MAX_LINE = 200  # a hard bound on what may reach the ui — the prompt asks for ~110
# output that is an error, a refusal or a hedge is not a directive — keep the old line.
REJECT = re.compile(r"api error|rate.?limit|usage limit|error:|i (can'?t|cannot)", re.I)

PROMPT = """you are mellen's sharp, no-fluff life coach. his one campaign is \
financial independence — his own income, his own place. his priority project is \
heatsync, a pre-launch threaded social platform for twitch/kick; shipping it is \
the lever for everything else. he responds to concrete moves and to being \
pointed in a clear direction — not coddling, not lists.

today is {today}. where he actually is right now:
{state}

what you know about him (dated notes — each is a snapshot of THAT day, not of \
now; anything more than a few days old is history, and the live state above \
outranks it):
{memory}

lines you already gave him (newest last). never repeat one, and never rephrase \
a dismissed one — he saw it and rejected it:
{recent}

write ONE line: the single highest-leverage thing for him to do or think about \
next, right now. hard limits: one sentence, max ~110 characters, all lowercase, \
plain text, no markdown, no emoji, no preamble. make it concrete and personal — \
name a real todo, appointment or win from the live state above. default to the \
next heatsync move unless something is overdue or time-sensitive. if the state \
has not moved in a way that changes his next move, output exactly `-` and \
nothing else — saying nothing beats saying it twice. output only the line."""


def _norm(line):
    """what "the same line" means for the repeat check — wording noise removed."""
    return re.sub(r"[^a-z0-9 ]+", "", (line or "").lower()).strip()


def fingerprint(state):
    """the state this directive was written for. the writer stays silent until
    this changes, so nine timer ticks over an unchanged day produce one line."""
    return hashlib.sha1(state.encode("utf-8")).hexdigest()[:16]


def _recent_block():
    lines = store.coach_recent_lines()
    cur = store.get_coach()
    if cur.get("line"):
        lines = lines + [{"line": cur["line"], "dismissed": bool(cur.get("dismissed"))}]
    if not lines:
        return "(none yet)"
    return "\n".join("- " + r["line"] + (" [dismissed]" if r.get("dismissed") else "")
                     for r in lines)


def ask(today, state):
    """one claude call. returns the cleaned line, "-" for nothing to say, or None
    when the output can't be trusted (caller keeps the previous line)."""
    prompt = PROMPT.format(today=today, state=state,
                           memory=coach_chat.memory_summary(today),
                           recent=_recent_block())
    try:
        r = subprocess.run([CLAUDE, "-p"], input=prompt, capture_output=True,
                           text=True, timeout=TIMEOUT, cwd="/tmp")
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"claude unavailable ({e}) — keeping the previous line")
        return None
    msg = " ".join((r.stdout or "").split())
    msg = re.sub(r"^[\"'`]+|[\"'`]+$", "", msg).strip()
    if msg == "-":
        return "-"
    if not msg or len(msg) > MAX_LINE or REJECT.search(msg):
        print("output rejected — keeping the previous line")
        return None
    # the anti-repeat guarantee lives here, not in the model's good intentions.
    if any(_norm(msg) == _norm(r["line"]) for r in store.coach_recent_lines()) \
            or _norm(msg) == _norm(store.get_coach().get("line", "")):
        print("same line as before — staying quiet")
        return "-"
    return msg


def push():
    """send the live line once, to the one channel. a dismissed or already-sent
    line never goes out — the push is the same voice as the ui, not a second one."""
    if not notify.configured():
        print("ntfy not configured — no push")
        return False
    line = store.coach_push_pending()
    if not line:
        print("nothing new to push")
        return False
    try:
        notify.send("coach", line, priority=3, tags=["muscle"], view="todos")
    except OSError as e:
        print(f"push failed ({e}) — unclaimed, retried next run")
        return False
    store.mark_coach_pushed()  # claimed only once it actually landed
    print("pushed:", line)
    return True


def main():
    dry = "--dry-run" in sys.argv
    today = date.today().isoformat()
    state = coach_chat.state_summary(today)
    fp = fingerprint(state)

    if store.coach_is_current(fp) and "--force" not in sys.argv:
        print("state unchanged — nothing to say")
    else:
        line = ask(today, state)
        if dry:
            print("dry run — " + ("would keep the previous line (no usable output)" if line is None
                                  else "would stay quiet (nothing new to say)" if line == "-"
                                  else f"would write: {line}"))
        elif line == "-":
            store.touch_coach(fp)  # judged, nothing new — don't ask again until it moves
        elif line:
            store.set_coach(line, fp)
            print("coach:", line)

    if "--notify" in sys.argv and not dry:
        push()


if __name__ == "__main__":
    main()
