@echo off
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

echo [1/3] Creating virtual environment...
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

echo [2/3] Activating virtual environment...
call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo ERROR: Failed to activate virtual environment
    pause
    exit /b 1
)
echo.

echo [3/3] Installing dependencies...
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Failed to install dependencies
    pause
    exit /b 1
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
echo   python ip_camera.py
echo.
pause
