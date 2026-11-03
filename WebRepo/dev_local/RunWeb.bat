@echo off
setlocal

REM --------------------------------------------------
REM Resolve paths
REM --------------------------------------------------
set "ROOT=%~dp0"
cd /d "%ROOT%.."

set "PROJ=%CD%"

REM --------------------------------------------------
REM Local HTTP development settings
REM --------------------------------------------------
set "APP_ENV=development"
set "AUTH_COOKIE_SECURE=false"
set "REDIS_URL=redis://127.0.0.1:6379"
REM Matches dev_local/turnserver.conf; TCP works with WSL localhost forwarding.
set "TURN_SHARED_SECRET=local-test-only"
set "TURN_URLS=turn:127.0.0.1:3478?transport=tcp"

REM --------------------------------------------------
REM Ensure Python can import `server`
REM --------------------------------------------------
set "PYTHONPATH=%PROJ%"

REM --------------------------------------------------
REM start redis server
REM --------------------------------------------------
echo Starting Capstone services inside WSL...
REM WSL inherits the current WebRepo directory; no machine-specific path is needed.
wsl.exe -d Ubuntu -- bash ./dev_local/start_server.sh
if errorlevel 1 (
    echo.
    echo Server startup failed.
    pause
    exit /b 1
)

REM Keep TURN in a live WSL process so WSL does not stop Redis while Python runs.
echo Starting local TURN on port 3478...
start "" /b wsl.exe -d Ubuntu -- turnserver -c ./dev_local/turnserver.conf

REM --------------------------------------------------
REM Start FastAPI (foreground)
REM --------------------------------------------------
echo Starting FastAPI server...
echo Website hosting on http://127.0.0.1:8000
"%PROJ%\..\.venv\Scripts\python.exe" -m uvicorn server.app:app ^
  --host 127.0.0.1 ^
  --port 8000 ^
  --workers 1 ^
  --lifespan on ^
  --log-level info

endlocal
