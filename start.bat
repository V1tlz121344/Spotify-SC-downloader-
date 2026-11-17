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
set "PYVER=3.12.10"
set "PYURL=https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-amd64.exe"
set "PYINST=%TEMP%\python-%PYVER%-amd64.exe"

rem ---- 1. installed Python 3.10-3.14 in standard locations ----
for %%V in (314 313 312 311 310) do (
    if not defined PY (
        for %%P in (
            "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
            "C:\Program Files\Python%%V\python.exe"
            "C:\Python%%V\python.exe"
        ) do (
            if not defined PY if exist "%%~P" (
                "%%~P" -c "import sys;raise SystemExit(0 if (3,9)<=sys.version_info[:2]<=(3,14) else 1)" >nul 2>&1
                if not errorlevel 1 set PY="%%~P"
            )
        )
    )
)

rem ---- 2. py launcher ----
if not defined PY (
    where py >nul 2>nul
    if not errorlevel 1 (
        for %%V in (3.12 3.13 3.14 3) do (
            if not defined PY (
                py -%%V -c "import sys;raise SystemExit(0 if (3,9)<=sys.version_info[:2]<=(3,14) else 1)" >nul 2>&1
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
                "%%P" -c "import sys;raise SystemExit(0 if (3,9)<=sys.version_info[:2]<=(3,14) else 1)" >nul 2>&1
                if not errorlevel 1 set PY="%%P"
            )
        )
    )
)

rem ---- 4. install Python 3.12 from python.org, if nothing usable was found ----
if not defined PY (
    echo Python 3.9-3.14 not found. Downloading Python %PYVER% from python.org...
    if exist "%PYINST%" del /q "%PYINST%" >nul 2>&1
    where curl.exe >nul 2>nul
    if not errorlevel 1 (
        curl.exe -L --fail --silent --show-error -o "%PYINST%" "%PYURL%"
    ) else (
        powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%PYURL%' -OutFile '%PYINST%' -UseBasicParsing"
    )
    if exist "%PYINST%" (
        echo Installing Python %PYVER%, 1-2 minutes...
        "%PYINST%" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_launcher=1
        del /q "%PYINST%" >nul 2>&1
        if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
            set PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        )
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
    echo Could not install Python 3.9-3.14 automatically.
    echo Install Python 3.12 manually: https://www.python.org/downloads/
    echo Then run start.bat again.
    pause
    exit /b 1
)

%PY% -u "%~dp0music_dl.py" %*
echo.
pause
