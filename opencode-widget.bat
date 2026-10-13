@echo off
rem ============================================================
rem  opencode go usage widget - one-click launcher
rem  Starts widget.pyw with pythonw, no console window.
rem  (Keep this file ASCII-only: cmd.exe parses it in the
rem   local ANSI code page, non-ASCII comments can break lines.)
rem ============================================================
setlocal
set "WIDGET=%~dp0widget.pyw"

if not exist "%WIDGET%" (
    echo [ERROR] widget.pyw not found: %WIDGET%
    pause
    exit /b 1
)

rem 1) pythonw.exe on PATH
where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw "%WIDGET%"
    exit /b 0
)

rem 2) Python launcher pyw.exe (default .pyw association)
if exist "%SystemRoot%\pyw.exe" (
    start "" "%SystemRoot%\pyw.exe" "%WIDGET%"
    exit /b 0
)

rem 3) fallback: open by file association
start "" "%WIDGET%"
exit /b 0
