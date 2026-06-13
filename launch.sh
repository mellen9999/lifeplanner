#!/usr/bin/env bash
# lifeplanner launcher (linux). starts the server (single-instance) + opens the ui.
cd "$(dirname "$0")" || exit 1
# prefer the venv if present (caldav sync needs icalendar/defusedxml); else stdlib.
exec "$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)" app.pyw
