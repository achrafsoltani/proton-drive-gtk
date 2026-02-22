#!/bin/bash
# Install Proton Drive GTK

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_FILE="$HOME/.local/share/applications/proton-drive-gtk.desktop"
AUTOSTART_FILE="$HOME/.config/autostart/proton-drive-gtk.desktop"
NAUTILUS_EXT_DIR="$HOME/.local/share/nautilus-python/extensions"
ICONS_DIR="$HOME/.local/share/icons/hicolor"

echo "=== Proton Drive GTK Installer ==="
echo ""

# Check dependencies
echo "[1/7] Checking dependencies..."

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
echo "[2/7] Creating virtual environment..."
if [ ! -d "$SCRIPT_DIR/venv" ]; then
    python3 -m venv "$SCRIPT_DIR/venv" --system-site-packages
    echo "Virtual environment created."
else
    echo "Virtual environment already exists."
fi

# Make run.sh executable
echo "[3/7] Setting permissions..."
chmod +x "$SCRIPT_DIR/run.sh"

# Install desktop entry
echo "[4/7] Installing desktop entry..."
mkdir -p "$(dirname "$DESKTOP_FILE")"

sed "s|{{INSTALL_PATH}}|$SCRIPT_DIR|g" \
    "$SCRIPT_DIR/assets/proton-drive-gtk.desktop" > "$DESKTOP_FILE"

echo "Desktop entry installed to: $DESKTOP_FILE"

# Install Nautilus extension
echo "[5/7] Installing Nautilus extension..."
if python3 -c "from gi.repository import Nautilus" 2>/dev/null; then
    # Try user directory first
    mkdir -p "$NAUTILUS_EXT_DIR"
    cp "$SCRIPT_DIR/nautilus/proton_drive_nautilus.py" "$NAUTILUS_EXT_DIR/"
    echo "Nautilus extension installed to: $NAUTILUS_EXT_DIR"
    echo "Restart Nautilus to activate: nautilus -q"
else
    echo "Warning: python3-nautilus not found. Nautilus extension not installed."
    echo "Install with: sudo apt install python3-nautilus"
fi

# Install emblem icons
echo "[6/7] Installing emblem icons..."
SIZES="16x16 22x22 24x24 32x32 48x48"
for size in $SIZES; do
    icon_dest="$ICONS_DIR/$size/emblems"
    mkdir -p "$icon_dest"
    if [ -d "$SCRIPT_DIR/assets/icons/emblems/$size" ]; then
        cp "$SCRIPT_DIR/assets/icons/emblems/$size/"*.png "$icon_dest/" 2>/dev/null || true
    fi
done
# Install scalable icons
mkdir -p "$ICONS_DIR/scalable/emblems"
cp "$SCRIPT_DIR/assets/icons/emblems/scalable/"*.svg "$ICONS_DIR/scalable/emblems/" 2>/dev/null || true

# Update icon cache
if command -v gtk-update-icon-cache &> /dev/null; then
    gtk-update-icon-cache -f -t "$ICONS_DIR" 2>/dev/null || true
fi
echo "Emblem icons installed."

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
echo "[7/7] Checking rclone configuration..."
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
