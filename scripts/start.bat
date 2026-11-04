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

setlocal
set "ROOT=%~dp0.."
pushd "%ROOT%"

REM Pick an interpreter. Not every Windows Python installs the py launcher,
REM and some expose python but not py (Microsoft Store builds, for example).
REM Order: PYTHON env var -> project .venv -> py -3 -> python -> python3.
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

if not defined PY (
    echo [env] No Python found on PATH. Install Python 3.11+ from
    echo       https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    echo       Already installed? Point the PYTHON environment variable at it.
    popd
    endlocal
    exit /b 1
)

%PY% -m steward start %*
set "RC=%ERRORLEVEL%"

popd
endlocal & exit /b %RC%
