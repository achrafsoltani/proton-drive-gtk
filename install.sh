#!/bin/bash
# Install Proton Drive GTK

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_FILE="$HOME/.local/share/applications/proton-drive-gtk.desktop"
AUTOSTART_FILE="$HOME/.config/autostart/proton-drive-gtk.desktop"

echo "=== Proton Drive GTK Installer ==="
echo ""

# Check dependencies
echo "[1/5] Checking dependencies..."

if ! command -v python3 &> /dev/null; then
    echo "Error: python3 is required but not installed."
    exit 1
fi

if ! command -v rclone &> /dev/null; then
    echo "Warning: rclone is not installed."
    echo "Install it with: sudo apt install rclone"
    echo "Or: curl https://rclone.org/install.sh | sudo bash"
fi

# Check GTK dependencies
if ! python3 -c "import gi; gi.require_version('Gtk', '3.0')" 2>/dev/null; then
    echo "Error: PyGObject (GTK) is required."
    echo "Install with: sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1"
    exit 1
fi

# Create virtual environment
echo "[2/5] Creating virtual environment..."
if [ ! -d "$SCRIPT_DIR/venv" ]; then
    python3 -m venv "$SCRIPT_DIR/venv" --system-site-packages
    echo "Virtual environment created."
else
    echo "Virtual environment already exists."
fi

# Make run.sh executable
echo "[3/5] Setting permissions..."
chmod +x "$SCRIPT_DIR/run.sh"

# Install desktop entry
echo "[4/5] Installing desktop entry..."
mkdir -p "$(dirname "$DESKTOP_FILE")"

sed "s|{{INSTALL_PATH}}|$SCRIPT_DIR|g" \
    "$SCRIPT_DIR/assets/proton-drive-gtk.desktop" > "$DESKTOP_FILE"

echo "Desktop entry installed to: $DESKTOP_FILE"

# Ask about autostart
echo ""
read -p "Enable autostart on login? [y/N] " -n 1 -r
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    mkdir -p "$(dirname "$AUTOSTART_FILE")"
    cp "$DESKTOP_FILE" "$AUTOSTART_FILE"
    echo "Autostart enabled."
fi

# Check rclone configuration
echo ""
echo "[5/5] Checking rclone configuration..."
if rclone listremotes 2>/dev/null | grep -q "protondrive:"; then
    echo "Proton Drive remote found."
else
    echo ""
    echo "Warning: 'protondrive' remote not configured in rclone."
    echo "Run 'rclone config' to set it up:"
    echo "  1. Select 'n' for new remote"
    echo "  2. Name it 'protondrive'"
    echo "  3. Select 'protondrive' as storage type"
    echo "  4. Enter your Proton credentials"
fi

echo ""
echo "=== Installation complete ==="
echo ""
echo "To run: ./run.sh"
echo "Or search 'Proton Drive' in your application menu."
