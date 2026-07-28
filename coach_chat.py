"""agentic coach chat — mellen talks to claude, which can act on the planner.

the web app shells out to the claude CLI with a stdio MCP server scoped to
lifeplanner's OWN tools (add/reschedule/complete/journal — never delete, never
shell/file access). so the coach can manipulate the planner's data and nothing
else. mele-local: claude + the venv + mcp_server.py all live beside this file.

bulletproof posture: allow-list of exactly the lifeplanner tools we want, an
explicit deny-list of the dangerous built-ins as defence-in-depth, a hard
timeout, capped message + history, and --strict-mcp-config so no ambient MCP
server (e.g. a project .mcp.json) can widen the tool surface. writes route
through the same store/FileLock the app uses, so a coach edit and a ui edit
can never corrupt each other.
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import review
import store

BASE = Path(__file__).resolve().parent
CLAUDE = os.environ.get("LIFEPLANNER_CLAUDE_BIN") or shutil.which("claude") or "claude"
PY = os.environ.get("LIFEPLANNER_PY") or sys.executable
MCP_SERVER = str(BASE / "mcp_server.py")
TIMEOUT = int(os.environ.get("LIFEPLANNER_COACH_TIMEOUT", "150"))
MAX_MSG = 2000
MAX_TURNS = 12  # most recent turns of history sent back for context

# exactly the tools the coach may use — reads + non-destructive writes. delete is
# deliberately absent: the coach creates and modifies, never destroys (data
# integrity is sacred; "cancel X" is served by muting/rescheduling instead).
_TOOLS = (
    "get_overview", "whats_slipping", "review_period", "get_stats", "get_day",
    "get_week", "get_range", "list_todos", "list_appointments",
    "list_achievements", "list_journal",
    "add_todo", "complete_todo", "update_todo",
    "add_appointment", "update_appointment",
    "add_achievement", "update_achievement",
    "add_journal", "update_journal",
)
ALLOWED = [f"mcp__lifeplanner__{t}" for t in _TOOLS]
# defence-in-depth: even though only ALLOWED is whitelisted, name the shell/file
# tools explicitly so a future config slip can't quietly grant them.
DISALLOWED = ["Bash", "Read", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch", "Task"]

_MCP_CONFIG = None


def _mcp_config_path():
    """write (once) the stdio MCP config that runs lifeplanner's own server. no
    secrets in it — just the python + script path — so it lives in the data dir."""
    global _MCP_CONFIG
    if _MCP_CONFIG and Path(_MCP_CONFIG).exists():
        return _MCP_CONFIG
    store._ensure()
    p = store.DATA / "coach-mcp.json"
    cfg = {"mcpServers": {"lifeplanner": {"command": PY, "args": [MCP_SERVER]}}}
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg), "utf-8")
    os.replace(tmp, p)
    _MCP_CONFIG = str(p)
    return _MCP_CONFIG


def _state_summary(today):
    """a compact grounding snapshot so the coach starts informed (it can still call
    tools for detail). kept terse to bound the prompt."""
    slip = review.whats_slipping()
    todos = store.list_items("todos")
    appts = [a for a in store.list_items("appointments") if not a.get("hidden")]
    open_ = [t for t in todos if not t.get("done")]
    parts = [f"open todos: {len(open_)}"]
    od = slip.get("overdue_todos") or []
    if od:
        parts.append("overdue: " + ", ".join(f'{t["title"]} ({t["days_late"]}d)' for t in od[:5]))
    rt = slip.get("routines_today") or []
    if rt:
        parts.append("routines left today: " + ", ".join(r["title"] for r in rt[:6]))
    soon = sorted((a for a in appts if store.next_occurrence(a, today)),
                  key=lambda a: store.next_occurrence(a, today))[:5]
    if soon:
        parts.append("next appts: " + ", ".join(
            f'{a["title"]} {store.next_occurrence(a, today)[:16]}' for a in soon))
    dsw = slip.get("days_since_win")
    if dsw:
        parts.append(f"{dsw}d since a logged win")
    return "\n".join(parts)


def _persona(today):
    return (
        "you are mellen's sharp, agentic life coach, living inside his lifeplanner "
        "app. you CAN act: you have the lifeplanner tools — use them to add, "
        "reschedule (update), complete, or journal on his behalf whenever he asks "
        "or it's clearly the right move. after you change something, state plainly "
        "and briefly what you did. never delete anything; to get an appointment off "
        "his calendar, reschedule it or set hidden=true (mute) instead. his one "
        "campaign is financial independence — his own income, his own place. his "
        "priority project is heatsync, a pre-launch threaded social platform for "
        "twitch/kick; shipping it is the lever. reply short, lowercase, plain text, "
        "no markdown, warm and direct. don't invent facts — check with a tool when "
        f"unsure. today is {today}."
    )


def _transcript(history, message):
    lines = []
    for turn in (history or [])[-MAX_TURNS:]:
        if not isinstance(turn, dict):
            continue
        role = "mellen" if turn.get("role") == "you" else "coach"
        text = str(turn.get("text") or "").strip()[:MAX_MSG]  # cap so a long turn can't bloat the prompt
        if text:
            lines.append(f"{role}: {text}")
    lines.append(f"mellen: {message}")
    lines.append("coach:")
    return "\n".join(lines)


def respond(message, history):
    """run one agentic turn. returns {"reply": str} or {"error": str}. never
    raises — every failure path degrades to a short spoken error."""
    message = (message or "").strip()[:MAX_MSG]
    if not message:
        return {"error": "empty message"}
    today = date.today().isoformat()
    prompt = (f"{_persona(today)}\n\ncurrent state:\n{_state_summary(today)}\n\n"
              f"conversation so far:\n{_transcript(history, message)}")
    cmd = [CLAUDE, "-p", prompt,
           "--mcp-config", _mcp_config_path(), "--strict-mcp-config",
           "--allowedTools", *ALLOWED, "--disallowedTools", *DISALLOWED]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=TIMEOUT, cwd="/tmp", stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"error": "coach timed out — try again"}
    except OSError as e:
        return {"error": f"coach unavailable ({e})"}
    out = (r.stdout or "").strip()
    if r.returncode != 0 and not out:
        return {"error": "coach failed — " + ((r.stderr or "").strip()[:200] or "no output")}
    if not out:
        return {"error": "coach had nothing to say — try rephrasing"}
    return {"reply": out}
