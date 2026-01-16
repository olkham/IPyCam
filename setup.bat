@echo off
setlocal enabledelayedexpansion
echo ===============================================
echo IPyCam Setup Script
echo ===============================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH
    echo Please install Python 3.8+ from https://www.python.org/
    pause
    exit /b 1
)

echo [1/4] Creating virtual environment...
if exist .venv (
    echo Virtual environment already exists, skipping creation
) else (
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment
        pause
        exit /b 1
    )
    echo Virtual environment created successfully
)
echo.

echo [2/4] Activating virtual environment...
call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo ERROR: Failed to activate virtual environment
    pause
    exit /b 1
)
echo.

echo [3/4] Installing dependencies...
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Failed to install dependencies
    pause
    exit /b 1
)
echo.

echo [4/4] Installing IPyCam package...
pip install -e .
if errorlevel 1 (
    echo ERROR: Failed to install IPyCam package
    pause
    exit /b 1
)
echo.

REM Check for go2rtc
echo [5/5] Checking for go2rtc...
if exist go2rtc.exe (
    echo go2rtc is already installed
) else (
    echo go2rtc is not installed
    echo.
    set /p "download=Would you like to download go2rtc? (Y/N): "
    if /i "!download!"=="y" (
        echo.
        echo Downloading go2rtc v1.9.9...
        powershell -NoProfile -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; (New-Object System.Net.WebClient).DownloadFile('https://github.com/AlexxIT/go2rtc/releases/download/v1.9.9/go2rtc_win64.zip', 'go2rtc_win64.zip'); Write-Host 'Download completed successfully' } catch { Write-Host 'ERROR: Failed to download go2rtc'; exit 1 }"
        if errorlevel 1 (
            echo ERROR: Failed to download go2rtc
            pause
            exit /b 1
        )
        echo.
        echo Extracting go2rtc...
        powershell -NoProfile -Command "try { Expand-Archive -Path 'go2rtc_win64.zip' -DestinationPath '.' -Force; Write-Host 'Extraction completed successfully'; Remove-Item 'go2rtc_win64.zip' } catch { Write-Host 'ERROR: Failed to extract go2rtc'; exit 1 }"
        if errorlevel 1 (
            echo ERROR: Failed to extract go2rtc
            pause
            exit /b 1
        )
        echo go2rtc has been installed
    ) else (
        echo Skipping go2rtc installation
    )
)
echo.

REM Check for ffmpeg
echo [6/6] Checking for ffmpeg...
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo ffmpeg is not installed
    echo.
    set /p "download_ffmpeg=Would you like to download ffmpeg? (Y/N): "
    if /i "!download_ffmpeg!"=="y" (
        echo.
        echo Downloading ffmpeg...
        powershell -NoProfile -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; (New-Object System.Net.WebClient).DownloadFile('https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip', 'ffmpeg-latest-win64.zip'); Write-Host 'Download completed successfully' } catch { Write-Host 'ERROR: Failed to download ffmpeg'; exit 1 }"
        if errorlevel 1 (
            echo ERROR: Failed to download ffmpeg
            pause
            exit /b 1
        )
        echo.
        echo Extracting ffmpeg...
        powershell -NoProfile -Command "try { Expand-Archive -Path 'ffmpeg-latest-win64.zip' -DestinationPath 'ffmpeg_temp' -Force; Get-ChildItem 'ffmpeg_temp' -Recurse -Filter 'ffmpeg.exe' | Move-Item -Destination '.'; Get-ChildItem 'ffmpeg_temp' -Recurse -Filter 'ffprobe.exe' | Move-Item -Destination '.'; Remove-Item 'ffmpeg_temp' -Recurse -Force; Remove-Item 'ffmpeg-latest-win64.zip' } catch { Write-Host 'ERROR: Failed to extract ffmpeg'; exit 1 }"
        if errorlevel 1 (
            echo ERROR: Failed to extract ffmpeg
            pause
            exit /b 1
        )
        echo ffmpeg has been installed
    ) else (
        echo Skipping ffmpeg installation
    )
) else (
    echo ffmpeg is already installed
)
echo.

echo ===============================================
echo Setup completed successfully!
echo ===============================================
echo.
echo To activate the environment, run:
echo   .venv\Scripts\activate
echo.
echo To run the camera, use:
echo   python -m ipycam
echo.
pause
