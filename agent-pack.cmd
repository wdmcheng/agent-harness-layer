@echo off
setlocal
set "PROJECT_DIR=%~dp0"
set "LOCK=%PROJECT_DIR%\.agents\agent-pack.lock.json"
if "%~1"=="migrate" if "%~2"=="--pack" if not "%~3"=="" set "PACK_DIR=%~3"
if not defined PACK_DIR for /f "delims=" %%i in ('python -c "import json, os, sys; p=sys.argv[1]; print(json.load(open(p, encoding='utf-8')).get('packPath','') if os.path.exists(p) else '')" "%LOCK%" 2^>NUL') do set "PACK_DIR=%%i"
if not defined PACK_DIR for /f "delims=" %%i in ('py -3 -c "import json, os, sys; p=sys.argv[1]; print(json.load(open(p, encoding='utf-8')).get('packPath','') if os.path.exists(p) else '')" "%LOCK%" 2^>NUL') do set "PACK_DIR=%%i"
if not defined PACK_DIR for /f "delims=" %%i in ('python3 -c "import json, os, sys; p=sys.argv[1]; print(json.load(open(p, encoding='utf-8')).get('packPath','') if os.path.exists(p) else '')" "%LOCK%" 2^>NUL') do set "PACK_DIR=%%i"
if not defined PACK_DIR echo 错误：缺少 .agents/agent-pack.lock.json，无法定位 Agent Pack。 1>&2 & exit /b 1
cd /d "%PROJECT_DIR%"
call "%PACK_DIR%\agent-pack.cmd" %*
exit /b %errorlevel%
