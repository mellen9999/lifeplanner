#!/usr/bin/env bash
# lifeplanner launcher (linux). starts the server (single-instance) + opens the ui.
cd "$(dirname "$0")" || exit 1
exec python3 app.pyw
