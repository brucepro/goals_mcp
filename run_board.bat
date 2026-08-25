@echo off
REM The board — a visual surface over the same goals.db the MCP writes to.
REM Stdlib only, no install step. Set BOARD_PASS to require a login.

setlocal
if "%GOAL_DB%"=="" set GOAL_DB=%~dp0goals.db
if "%BOARD_PORT%"=="" set BOARD_PORT=8077

if not exist "%GOAL_DB%" (
  echo No database at %GOAL_DB%
  echo It is created on first connect - start the MCP server once, or set GOAL_DB.
  exit /b 1
)

echo Board on http://127.0.0.1:%BOARD_PORT%/   db: %GOAL_DB%
if "%BOARD_PASS%"=="" echo No BOARD_PASS set - auth disabled, listening on localhost only.

start "" http://127.0.0.1:%BOARD_PORT%/
python "%~dp0board_server.py" --port %BOARD_PORT% --host 127.0.0.1
endlocal
