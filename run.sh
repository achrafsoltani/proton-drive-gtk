#!/bin/bash
# Run Proton Drive GTK from virtual environment

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
LOG_FILE="/tmp/proton-drive-gtk.log"

# Parse arguments
FOREGROUND=false
LEGACY=false
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
    case $1 in
        --fg|--foreground)
            FOREGROUND=true
            shift
            ;;
        --legacy|--vfs)
            LEGACY=true
            shift
            ;;
        *)
            EXTRA_ARGS+=("$1")
            shift
            ;;
    esac
done

# Activate venv
source "$VENV_DIR/bin/activate"

# Build command
CMD=(python3 "$SCRIPT_DIR/src/main.py")
if [ "$LEGACY" = true ]; then
    CMD+=(--legacy)
fi
CMD+=("${EXTRA_ARGS[@]}")

# Run
if [ "$FOREGROUND" = true ]; then
    "${CMD[@]}"
else
    nohup "${CMD[@]}" > "$LOG_FILE" 2>&1 &
    disown
    echo "Proton Drive GTK started (PID: $!)"
    echo "Log: $LOG_FILE"
fi
