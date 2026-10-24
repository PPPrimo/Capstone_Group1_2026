@echo off
setlocal

rem Dev-only helper: generate a locally-trusted TLS cert using mkcert.
rem Usage:
rem   generate_cert.bat "C:\path\server.crt" "C:\path\server.key"

set "CERT_FILE=%~1"
set "KEY_FILE=%~2"

if "%CERT_FILE%"=="" exit /b 2
if "%KEY_FILE%"=="" exit /b 2

where mkcert >nul 2>nul
if errorlevel 1 (
  echo mkcert not found on PATH.
  echo Install mkcert or set USE_MKCERT=0 in RunWeb.bat.
  exit /b 1
)

mkcert -cert-file "%CERT_FILE%" -key-file "%KEY_FILE%" localhost 127.0.0.1
exit /b %ERRORLEVEL%
