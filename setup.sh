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

echo "[1/4] Creating virtual environment..."
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

echo "[2/4] Activating virtual environment..."
source .venv/bin/activate
if [ $? -ne 0 ]; then
    echo "ERROR: Failed to activate virtual environment"
    exit 1
fi
echo

echo "[3/4] Installing dependencies..."
python -m pip install --upgrade pip
pip install -r requirements.txt
if [ $? -ne 0 ]; then
    echo "ERROR: Failed to install dependencies"
    exit 1
fi
echo

echo "[4/4] Installing IPyCam package..."
pip install -e .
if [ $? -ne 0 ]; then
    echo "ERROR: Failed to install IPyCam package"
    exit 1
fi
echo

# Detect OS and architecture for go2rtc
detect_platform() {
    OS=$(uname -s | tr '[:upper:]' '[:lower:]')
    ARCH=$(uname -m)
    
    case "$OS" in
        linux*)
            case "$ARCH" in
                x86_64) echo "go2rtc_linux_amd64" ;;
                aarch64|arm64) echo "go2rtc_linux_arm64" ;;
                armv7l) echo "go2rtc_linux_arm" ;;
                armv6l) echo "go2rtc_linux_armv6" ;;
                i386|i686) echo "go2rtc_linux_i386" ;;
                *) echo "" ;;
            esac
            ;;
        darwin*)
            case "$ARCH" in
                x86_64) echo "go2rtc_mac_amd64.zip" ;;
                arm64) echo "go2rtc_mac_arm64.zip" ;;
                *) echo "" ;;
            esac
            ;;
        *) echo "" ;;
    esac
}

# Check for go2rtc
echo "[5/6] Checking for go2rtc..."
if [ -f "go2rtc" ] || [ -f "go2rtc.exe" ]; then
    echo "go2rtc is already installed"
else
    echo "go2rtc is not installed"
    echo
    read -p "Would you like to download go2rtc? (Y/N): " download
    if [ "${download,,}" = "y" ]; then
        PLATFORM=$(detect_platform)
        if [ -z "$PLATFORM" ]; then
            echo "ERROR: Unsupported platform $(uname -s)/$(uname -m)"
            echo "Please download go2rtc manually from https://github.com/AlexxIT/go2rtc/releases"
        else
            echo
            echo "Downloading go2rtc v1.9.9 for $PLATFORM..."
            GO2RTC_URL="https://github.com/AlexxIT/go2rtc/releases/download/v1.9.9/$PLATFORM"
            
            if command -v curl &> /dev/null; then
                curl -L -o "go2rtc_download" "$GO2RTC_URL"
            elif command -v wget &> /dev/null; then
                wget -O "go2rtc_download" "$GO2RTC_URL"
            else
                echo "ERROR: Neither curl nor wget found. Please install one of them."
                exit 1
            fi
            
            if [ $? -ne 0 ]; then
                echo "ERROR: Failed to download go2rtc"
                exit 1
            fi
            
            echo "Extracting go2rtc..."
            if [[ "$PLATFORM" == *.zip ]]; then
                unzip -o "go2rtc_download" && rm "go2rtc_download"
            else
                mv "go2rtc_download" "go2rtc"
            fi
            
            chmod +x go2rtc
            echo "go2rtc has been installed"
        fi
    else
        echo "Skipping go2rtc installation"
    fi
fi
echo

# Check for ffmpeg
echo "[6/6] Checking for ffmpeg..."
if command -v ffmpeg &> /dev/null; then
    echo "ffmpeg is already installed"
else
    echo "ffmpeg is not installed"
    echo
    read -p "Would you like to install ffmpeg via package manager? (Y/N): " download_ffmpeg
    if [ "${download_ffmpeg,,}" = "y" ]; then
        echo
        echo "Installing ffmpeg..."
        
        # Detect package manager and install
        if command -v apt-get &> /dev/null; then
            sudo apt-get update && sudo apt-get install -y ffmpeg
        elif command -v yum &> /dev/null; then
            sudo yum install -y ffmpeg
        elif command -v dnf &> /dev/null; then
            sudo dnf install -y ffmpeg
        elif command -v pacman &> /dev/null; then
            sudo pacman -S --noconfirm ffmpeg
        elif command -v brew &> /dev/null; then
            brew install ffmpeg
        else
            echo "ERROR: No supported package manager found"
            echo "Please install ffmpeg manually from https://ffmpeg.org/"
            exit 1
        fi
        
        if [ $? -eq 0 ]; then
            echo "ffmpeg has been installed"
        else
            echo "ERROR: Failed to install ffmpeg"
            exit 1
        fi
    else
        echo "Skipping ffmpeg installation"
        echo "Note: You can install it later with your package manager:"
        echo "  Ubuntu/Debian: sudo apt-get install ffmpeg"
        echo "  Fedora: sudo dnf install ffmpeg"
        echo "  Arch: sudo pacman -S ffmpeg"
        echo "  macOS: brew install ffmpeg"
    fi
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
echo "  python -m ipycam"
echo
