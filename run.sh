#!/bin/bash
# Run Proton Drive GTK from virtual environment

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"

# Activate venv
source "$VENV_DIR/bin/activate"

# Run in background by default, use --fg for foreground
if [[ "$1" == "--fg" ]]; then
    python3 "$SCRIPT_DIR/src/main.py"
else
    nohup python3 "$SCRIPT_DIR/src/main.py" > /dev/null 2>&1 &
    disown
    echo "Proton Drive GTK started (PID: $!)"
fi
