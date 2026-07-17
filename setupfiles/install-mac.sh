#!/bin/bash
# Captain installer for macOS.
# Creates a virtualenv, installs dependencies, and registers a Scripts menu
# entry (Captain.lua) so you can launch from Workspace → Scripts → Captain.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="$HOME/Library/Application Support/Captain"
VENV_DIR="$APP_DIR/.venv"
USER_SCRIPTS="$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
SYSTEM_SCRIPTS="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"

echo "Captain installer"
echo "App directory: $APP_DIR"

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

echo
echo "Creating virtualenv..."
"$PYTHON" -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip
echo "Installing dependencies (this downloads ML libraries; may take a while)..."
"$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt"

echo
echo "Registering Captain with DaVinci Resolve (Workspace → Scripts → Captain)..."
mkdir -p "$USER_SCRIPTS"
cp "$APP_DIR/scripts/Captain.lua" "$USER_SCRIPTS/Captain.lua"

# One menu entry only — remove legacy probes / duplicates.
rm -f "$USER_SCRIPTS/HelloCaptain.lua" \
      "$USER_SCRIPTS/CaptainPython.py" \
      "$USER_SCRIPTS/Captain.py"
if [ -d "$SYSTEM_SCRIPTS" ]; then
    rm -f "$SYSTEM_SCRIPTS/Captain.lua" \
          "$SYSTEM_SCRIPTS/HelloCaptain.lua" \
          "$SYSTEM_SCRIPTS/CaptainPython.py" \
          "$SYSTEM_SCRIPTS/Captain.py" 2>/dev/null || \
    sudo rm -f "$SYSTEM_SCRIPTS/Captain.lua" \
               "$SYSTEM_SCRIPTS/HelloCaptain.lua" \
               "$SYSTEM_SCRIPTS/CaptainPython.py" \
               "$SYSTEM_SCRIPTS/Captain.py" 2>/dev/null || true
fi

mkdir -p "$DATA_DIR"
cat > "$DATA_DIR/install.json" <<EOF
{
  "python": "$VENV_DIR/bin/python",
  "app_dir": "$APP_DIR"
}
EOF

echo
echo "Done."
echo "  1. Fully quit and reopen DaVinci Resolve"
echo "  2. Open a project"
echo "  3. Workspace → Scripts → Captain"
echo
echo "(First transcription downloads the Whisper model; later runs are fast.)"
