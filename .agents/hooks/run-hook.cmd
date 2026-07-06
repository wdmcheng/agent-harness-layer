@echo off
setlocal

if "%~1"=="" (
  echo Usage: run-hook.cmd ^<hook-name^> 1>&2
  exit /b 1
)

set "HOOK_NAME=%~1"
set "AGENT_NAME=%~2"
if not defined AGENT_NAME set "AGENT_NAME=%AGENT_PACK_AGENT%"
set "PROJECT_DIR=%AGENT_PACK_PROJECT_DIR%"
if not defined PROJECT_DIR set "PROJECT_DIR=%CLAUDE_PROJECT_DIR%"
if not defined PROJECT_DIR set "PROJECT_DIR=%CODEX_PROJECT_DIR%"
if not defined PROJECT_DIR for /f "delims=" %%i in ('git rev-parse --show-toplevel 2^>NUL') do set "PROJECT_DIR=%%i"
if not defined PROJECT_DIR set "PROJECT_DIR=%CD%"

set "RUNNER=%PROJECT_DIR%\.agents\hooks\agent_pack_hook.py"
if not exist "%RUNNER%" (
  echo 错误：找不到 Agent Pack hook runner：%RUNNER% 1>&2
  exit /b 1
)

python "%RUNNER%" "%HOOK_NAME%" "%AGENT_NAME%"
if not errorlevel 9009 exit /b %errorlevel%

py -3 "%RUNNER%" "%HOOK_NAME%" "%AGENT_NAME%"
if not errorlevel 9009 exit /b %errorlevel%

python3 "%RUNNER%" "%HOOK_NAME%" "%AGENT_NAME%"
if not errorlevel 9009 exit /b %errorlevel%

echo 错误：需要 Python 3 来运行 Agent Pack hooks。 1>&2
exit /b 1
