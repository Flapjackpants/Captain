#!/bin/bash
# Captain installer for macOS.
# Creates a virtualenv, installs dependencies, registers the launcher with
# DaVinci Resolve, and records the install location for the launcher.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="$HOME/Library/Application Support/Captain"
VENV_DIR="$APP_DIR/.venv"
RESOLVE_SCRIPTS="$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"

echo "Captain installer"
echo "App directory: $APP_DIR"

# Pick a Python: prefer 3.11/3.12/3.13 (faster-whisper wheel coverage).
PYTHON=""
for candidate in python3.12 python3.13 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON="$(command -v "$candidate")"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "ERROR: No python3 found. Install Python 3.11+ (e.g. brew install python@3.12)."
    exit 1
fi
echo "Using Python: $PYTHON ($($PYTHON --version))"

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "WARNING: ffmpeg not found on PATH. Install it with: brew install ffmpeg"
fi

echo "Creating virtualenv..."
"$PYTHON" -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip
echo "Installing dependencies (this downloads ML libraries; may take a while)..."
"$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "Registering launcher with DaVinci Resolve..."
mkdir -p "$RESOLVE_SCRIPTS"
cp "$APP_DIR/scripts/Captain.py" "$RESOLVE_SCRIPTS/Captain.py"

mkdir -p "$DATA_DIR"
cat > "$DATA_DIR/install.json" <<EOF
{
  "python": "$VENV_DIR/bin/python",
  "app_dir": "$APP_DIR"
}
EOF

echo
echo "Done. In DaVinci Resolve: Workspace > Scripts > Captain"
echo "(The first transcription downloads the Whisper model; later runs are fast.)"
