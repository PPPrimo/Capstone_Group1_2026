@echo off
setlocal

REM --------------------------------------------------
REM Resolve paths
REM --------------------------------------------------
set ROOT=%~dp0
cd /d %ROOT%\..

set PROJ=%CD%
set NGINX_EXE=%PROJ%\server\nginx\nginx.exe
set NGINX_PREFIX=%PROJ%\server\nginx
set NGINX_CONF=%ROOT%\custome_nginx_server.conf

REM --------------------------------------------------
REM Kill any running nginx
REM --------------------------------------------------
echo Killing nginx if running...
taskkill /IM nginx.exe /F >nul 2>&1

REM --------------------------------------------------
REM Start nginx (detached)
REM --------------------------------------------------
echo Starting nginx...
start "nginx" "%NGINX_EXE%" -p "%NGINX_PREFIX%" -c "%NGINX_CONF%"

REM --------------------------------------------------
REM Ensure Python can import `server`
REM --------------------------------------------------
set PYTHONPATH=%PROJ%

REM --------------------------------------------------
REM start redis server
REM --------------------------------------------------
echo Starting Capstone services inside WSL...
wsl.exe -d Ubuntu -- bash -lc "cd '/mnt/c/Users/tiant/Documents/1_My_folder/Projects/Capstone' && ./WebRepo/dev_local/start_server.sh"
if errorlevel 1 (
    echo.
    echo Server startup failed.
    pause
    exit /b 1
)

REM --------------------------------------------------
REM Start FastAPI (foreground)
REM --------------------------------------------------
echo Starting FastAPI server...
echo Website hosting on http://127.0.0.1:8000
python -m uvicorn server.app:app ^
  --host 127.0.0.1 ^
  --port 8000 ^
  --workers 1 ^
  --lifespan on ^
  --log-level info

endlocal
