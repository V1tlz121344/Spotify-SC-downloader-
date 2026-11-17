@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Music Downloader - SoundCloud / Spotify

if not exist "%~dp0music_dl.py" (
    echo [ERROR] music_dl.py not found next to this file.
    pause
    exit /b 1
)

set "PY="

rem ---- 1. installed Python 3.10-3.13 in standard locations ----
for %%V in (313 312 311 310) do (
    if not defined PY (
        for %%P in (
            "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
            "C:\Program Files\Python%%V\python.exe"
            "C:\Python%%V\python.exe"
        ) do (
            if not defined PY if exist "%%~P" (
                "%%~P" -c "import sys;raise SystemExit(0 if (3,9)<=sys.version_info[:2]<=(3,13) else 1)" >nul 2>&1
                if not errorlevel 1 set PY="%%~P"
            )
        )
    )
)

rem ---- 2. py launcher ----
if not defined PY (
    where py >nul 2>nul
    if not errorlevel 1 (
        for %%V in (3.12 3.13 3) do (
            if not defined PY (
                py -%%V -c "import sys;raise SystemExit(0 if (3,9)<=sys.version_info[:2]<=(3,13) else 1)" >nul 2>&1
                if not errorlevel 1 set "PY=py -%%V"
            )
        )
    )
)

rem ---- 3. python.exe from PATH, skipping the Microsoft Store stub ----
if not defined PY (
    for /f "delims=" %%P in ('where python.exe 2^>nul') do (
        if not defined PY (
            echo "%%P" | findstr /i "WindowsApps" >nul
            if errorlevel 1 (
                "%%P" -c "import sys;raise SystemExit(0 if (3,9)<=sys.version_info[:2]<=(3,13) else 1)" >nul 2>&1
                if not errorlevel 1 set PY="%%P"
            )
        )
    )
)

rem ---- 4. install Python 3.12 once, if nothing usable was found ----
if not defined PY (
    echo Python 3.9-3.13 not found. Installing Python 3.12 via winget, one time only...
    where winget >nul 2>nul
    if errorlevel 1 (
        echo winget not available. Install Python manually: https://www.python.org/downloads/
        pause
        exit /b 1
    )
    winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
        set PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    )
    if not defined PY if exist "C:\Program Files\Python312\python.exe" (
        set PY="C:\Program Files\Python312\python.exe"
    )
    if not defined PY (
        where py >nul 2>nul
        if not errorlevel 1 (
            py -3.12 -c "import sys" >nul 2>&1
            if not errorlevel 1 set "PY=py -3.12"
        )
    )
)

if not defined PY (
    echo.
    echo Could not find or install Python 3.9-3.13.
    echo Install it manually: winget install -e --id Python.Python.3.12
    pause
    exit /b 1
)

%PY% -u "%~dp0music_dl.py" %*
echo.
pause
