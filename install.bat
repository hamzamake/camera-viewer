@echo off
setlocal enabledelayedexpansion
title Camera Recorder - Installer
color 0A

echo.
echo  ============================================
echo    CAMERA RECORDER - INSTALLER
echo  ============================================
echo.

:: ── Check running as admin (needed for winget + system PATH edits) ────────────
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo  [!] This installer needs to run as Administrator.
    echo      Right-click the .bat and choose "Run as administrator".
    echo.
    pause
    exit /b 1
)

:: ─────────────────────────────────────────────────────────────────────────────
:: STEP 1 — Python
:: ─────────────────────────────────────────────────────────────────────────────
echo  [1/5] Checking Python...

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo        Python not found. Installing via winget...
    winget install --id Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements
    if %errorlevel% neq 0 (
        echo.
        echo  [ERROR] winget failed to install Python.
        echo          Please install Python 3.12+ manually from https://python.org
        echo          Make sure to tick "Add Python to PATH" during install.
        pause
        exit /b 1
    )
    :: Refresh PATH so python is usable in this session
    for /f "tokens=*" %%i in ('where python 2^>nul') do set PYTHON_EXE=%%i
    if "!PYTHON_EXE!"=="" (
        echo.
        echo  [!] Python installed but not on PATH yet.
        echo      Please close this window, reopen as admin, and run again.
        pause
        exit /b 1
    )
    echo        Python installed OK.
) else (
    for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo        Found: %%v
)

:: ─────────────────────────────────────────────────────────────────────────────
:: STEP 2 — pip (upgrade to latest)
:: ─────────────────────────────────────────────────────────────────────────────
echo.
echo  [2/5] Upgrading pip...
python -m pip install --upgrade pip --quiet
if %errorlevel% neq 0 (
    echo  [!] pip upgrade failed - continuing anyway.
) else (
    echo        pip up to date.
)

:: ─────────────────────────────────────────────────────────────────────────────
:: STEP 3 — Python packages (opencv + pillow)
:: ─────────────────────────────────────────────────────────────────────────────
echo.
echo  [3/5] Installing Python packages (opencv-python, Pillow)...

:: Check if requirements.txt is next to this bat file
set "SCRIPT_DIR=%~dp0"
set "REQ_FILE=%SCRIPT_DIR%requirements.txt"

if exist "%REQ_FILE%" (
    echo        Found requirements.txt — installing from it...
    python -m pip install -r "%REQ_FILE%" --quiet
) else (
    echo        requirements.txt not found — installing packages directly...
    python -m pip install opencv-python Pillow --quiet
)

if %errorlevel% neq 0 (
    echo.
    echo  [ERROR] Failed to install Python packages.
    echo          Check your internet connection and try again.
    pause
    exit /b 1
)
echo        Python packages installed OK.

:: ─────────────────────────────────────────────────────────────────────────────
:: STEP 4 — ffmpeg (via winget — gets the real static build, not a pip shim)
:: ─────────────────────────────────────────────────────────────────────────────
echo.
echo  [4/5] Checking ffmpeg...

ffmpeg -version >nul 2>&1
if %errorlevel% equ 0 (
    for /f "tokens=3" %%v in ('ffmpeg -version 2^>^&1 ^| findstr "ffmpeg version"') do (
        echo        Already installed: ffmpeg %%v
    )
    goto ffmpeg_done
)

echo        ffmpeg not found. Installing via winget...
winget install --id Gyan.FFmpeg -e --silent --accept-package-agreements --accept-source-agreements
if %errorlevel% neq 0 (
    echo.
    echo  [!] winget install of ffmpeg failed. Trying fallback (ffmpeg-python shim)...
    python -m pip install ffmpeg-python --quiet
    echo.
    echo  ┌─────────────────────────────────────────────────────────────┐
    echo  │  MANUAL STEP REQUIRED                                       │
    echo  │                                                             │
    echo  │  winget couldn't install ffmpeg automatically.              │
    echo  │  Please do ONE of the following:                            │
    echo  │                                                             │
    echo  │  Option A (easiest):                                        │
    echo  │    1. Go to  https://www.gyan.dev/ffmpeg/builds/            │
    echo  │    2. Download "ffmpeg-release-essentials.zip"              │
    echo  │    3. Extract it to  C:\ffmpeg                              │
    echo  │    4. Add  C:\ffmpeg\bin  to your system PATH               │
    echo  │       (System Properties > Environment Variables)           │
    echo  │                                                             │
    echo  │  Option B:                                                  │
    echo  │    Run PowerShell as Admin and run:                         │
    echo  │    winget install Gyan.FFmpeg                               │
    echo  └─────────────────────────────────────────────────────────────┘
    echo.
    pause
    goto ffmpeg_done
)

:: winget installs to a new PATH entry — refresh it for this session
set "FFMPEG_DEFAULT=C:\Program Files\ffmpeg\bin"
if exist "%FFMPEG_DEFAULT%\ffmpeg.exe" (
    set "PATH=%PATH%;%FFMPEG_DEFAULT%"
)

:: Also try the common local path
set "FFMPEG_LOCAL=C:\ffmpeg\bin"
if exist "%FFMPEG_LOCAL%\ffmpeg.exe" (
    set "PATH=%PATH%;%FFMPEG_LOCAL%"
)

echo        ffmpeg installed OK.
echo  [!]  NOTE: You may need to restart your PC once for ffmpeg
echo       to be available on the PATH in all programs.

:ffmpeg_done

:: ─────────────────────────────────────────────────────────────────────────────
:: STEP 5 — Final verification
:: ─────────────────────────────────────────────────────────────────────────────
echo.
echo  [5/5] Verifying installation...
echo.

set ALL_OK=1

python -c "import cv2; print('        opencv-python  ' + cv2.__version__ + '  OK')" 2>nul
if %errorlevel% neq 0 ( echo        [FAIL] opencv-python not importable & set ALL_OK=0 )

python -c "from PIL import Image; import PIL; print('        Pillow         ' + PIL.__version__ + '  OK')" 2>nul
if %errorlevel% neq 0 ( echo        [FAIL] Pillow not importable & set ALL_OK=0 )

ffmpeg -version >nul 2>&1
if %errorlevel% equ 0 (
    for /f "tokens=3" %%v in ('ffmpeg -version 2^>^&1 ^| findstr "ffmpeg version"') do (
        echo        ffmpeg         %%v  OK
    )
) else (
    echo        [FAIL] ffmpeg not on PATH - see manual step above
    set ALL_OK=0
)

ffprobe -version >nul 2>&1
if %errorlevel% equ 0 (
    echo        ffprobe        OK
) else (
    echo        [WARN] ffprobe not on PATH ^(comes with ffmpeg^)
)

echo.
if "!ALL_OK!"=="1" (
    color 0A
    echo  ============================================
    echo    ALL CHECKS PASSED - Ready to run!
    echo  ============================================
    echo.
    echo  Run the app with:
    echo    python recorder.py
    echo.
) else (
    color 0E
    echo  ============================================
    echo    SOME CHECKS FAILED - See messages above
    echo  ============================================
    echo.
    echo  Fix any [FAIL] items above then re-run this installer.
    echo.
)

pause
endlocal