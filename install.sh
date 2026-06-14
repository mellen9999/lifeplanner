#!/usr/bin/env bash
# one-shot setup: creates a self-contained venv for the optional mcp server,
# then prints the command to wire it into claude. the web app itself needs nothing
# but python 3 — you can skip this entirely and just run ./launch.sh.
set -euo pipefail
cd "$(dirname "$0")"
here="$(pwd)"

echo "==> creating venv (.venv) + installing mcp sdk"
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

cat <<EOF

lifeplanner is ready.

  start the app:      ./launch.sh        (or: python3 app.pyw)
  it opens:           http://127.0.0.1:8765

let an llm in (optional) — claude code / claude desktop:

  claude mcp add lifeplanner -s user -- "$here/.venv/bin/python" "$here/mcp_server.py"

  then restart claude and check with /mcp. it can now log your wins, add todos,
  and review your day — writing to the same local files the web app reads.

EOF
