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

set "PY="
if defined PYTHON set "PY=%PYTHON%"
if not defined PY set "PY=py -3"

if not defined PY (
    echo [env] Python not found. Install Python 3.11+ from https://www.python.org/downloads/
    popd
    endlocal
    exit /b 1
)

REM Prefer the project virtualenv when there is one.
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

%PY% -m steward start %*
set "RC=%ERRORLEVEL%"

popd
endlocal & exit /b %RC%
