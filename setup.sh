#!/bin/bash

echo "==============================================="
echo "IPyCam Setup Script"
echo "==============================================="
echo

# Check if Python is installed
if ! command -v python3 &> /dev/null; then
    echo "ERROR: Python 3 is not installed or not in PATH"
    echo "Please install Python 3.8+ from your package manager"
    exit 1
fi

echo "[1/3] Creating virtual environment..."
if [ -d ".venv" ]; then
    echo "Virtual environment already exists, skipping creation"
else
    python3 -m venv .venv
    if [ $? -ne 0 ]; then
        echo "ERROR: Failed to create virtual environment"
        exit 1
    fi
    echo "Virtual environment created successfully"
fi
echo

echo "[2/3] Activating virtual environment..."
source .venv/bin/activate
if [ $? -ne 0 ]; then
    echo "ERROR: Failed to activate virtual environment"
    exit 1
fi
echo

echo "[3/3] Installing dependencies..."
python -m pip install --upgrade pip
pip install -r requirements.txt
if [ $? -ne 0 ]; then
    echo "ERROR: Failed to install dependencies"
    exit 1
fi
echo

echo "==============================================="
echo "Setup completed successfully!"
echo "==============================================="
echo
echo "To activate the environment, run:"
echo "  source .venv/bin/activate"
echo
echo "To run the camera, use:"
echo "  python ip_camera.py"
echo
