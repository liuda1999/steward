@echo off
REM SPDX-License-Identifier: Apache-2.0
REM
REM steward launcher for Windows.
REM
REM Same job as scripts/start.sh: check Python, make sure deps are there,
REM prepare config.yaml, then start the gateway.
REM
REM   scripts\start.bat
REM   scripts\start.bat --port 27419
REM   scripts\start.bat --host 0.0.0.0
REM   scripts\start.bat --check
REM
REM NOTE: keep this file ASCII-only. A .bat file is read by cmd.exe using the
REM console codepage, so non-ASCII text can get mangled into mojibake.
REM Chinese docs live in README.md instead.

setlocal EnableExtensions
set "ROOT=%~dp0.."
pushd "%ROOT%"

REM Pick an interpreter. Not every Windows Python installs the py launcher,
REM and some expose python but not py (Microsoft Store builds, for example).
REM Order: PYTHON env var -> project .venv -> py -3 -> python -> python3
REM        -> common install dirs that were never added to PATH.
set "PY="
if defined PYTHON set "PY=%PYTHON%"

if not defined PY if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

if not defined PY (
    py -3 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    python -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    python3 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY=python3"
)

REM PATH is often missing Python because the installer was run without
REM "Add to PATH". Look one level into the usual locations before giving up.
if not defined PY call :scan "%LOCALAPPDATA%\Programs\Python\Python3*"
if not defined PY call :scan "%ProgramFiles%\Python3*"
if not defined PY call :scan "C:\Python3*"
if not defined PY call :scan "%USERPROFILE%\.workbuddy\binaries\python\versions\*"

if not defined PY (
    echo [env] No working Python 3.11+ found.
    echo       Install it from https://www.python.org/downloads/
    echo       and tick "Add python.exe to PATH", then run this again.
    echo       Already installed somewhere else? Tell the launcher:
    echo           set PYTHON=C:\path\to\python.exe
    popd
    endlocal
    exit /b 1
)

REM Quote the interpreter when it is a path (C:\...); "py -3" must stay
REM unquoted, otherwise cmd looks for a program literally named "py -3".
set "PYQ=%PY%"
if "%PY:~1,1%"==":" set "PYQ="%PY%""

%PYQ% -m steward start %*
set "RC=%ERRORLEVEL%"

popd
endlocal & exit /b %RC%

:scan
REM Usage: call :scan "C:\dir\Python3*" -- first working interpreter wins.
for /d %%D in ("%~1") do (
    if exist "%%~D\python.exe" (
        "%%~D\python.exe" -c "import sys" >nul 2>nul
        if not errorlevel 1 (
            set "PY=%%~D\python.exe"
            goto :eof
        )
    )
)
goto :eof
