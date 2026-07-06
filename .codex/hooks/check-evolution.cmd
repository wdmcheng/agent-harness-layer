@echo off
setlocal
set "HOOK_NAME=%~n0"
set "PROJECT_DIR=%AGENT_PACK_PROJECT_DIR%"
if not defined PROJECT_DIR set "PROJECT_DIR=%CODEX_PROJECT_DIR%"
if not defined PROJECT_DIR for /f "delims=" %%i in ('git rev-parse --show-toplevel 2^>NUL') do set "PROJECT_DIR=%%i"
if not defined PROJECT_DIR set "PROJECT_DIR=%CD%"
call "%PROJECT_DIR%\.agents\hooks\run-hook.cmd" "%HOOK_NAME%" codex
exit /b %errorlevel%
