@echo off
rem one-shot setup for windows: creates a venv for the optional mcp server and
rem prints the command to wire it into claude. the web app itself needs nothing
rem but python 3 — you can skip this and just run launch.bat.
setlocal
cd /d "%~dp0"

echo ==^> creating venv (.venv) + installing mcp sdk
python -m venv .venv
.venv\Scripts\python -m pip install --quiet --upgrade pip
.venv\Scripts\pip install --quiet -r requirements.txt

echo.
echo lifeplanner is ready.
echo.
echo   start the app:   launch.bat        (opens http://127.0.0.1:8765)
echo.
echo let an llm in (optional) — run this, then restart claude and check /mcp:
echo.
echo   claude mcp add lifeplanner -s user -- "%cd%\.venv\Scripts\python.exe" "%cd%\mcp_server.py"
echo.
endlocal
