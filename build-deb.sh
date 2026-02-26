#!/bin/bash
# Build .deb package for proton-drive-gtk

set -e

VERSION="1.1.0"
PACKAGE="proton-drive-gtk"
ARCH="amd64"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/${PACKAGE}_${VERSION}_${ARCH}"

echo "=== Building ${PACKAGE} ${VERSION} ==="

# Build Go daemon if Go is available
if command -v go &> /dev/null; then
    echo "Building Go sync daemon..."
    cd "$SCRIPT_DIR/go-daemon"
    mkdir -p bin
    CGO_ENABLED=0 go build -ldflags="-s -w" -o bin/proton-sync-daemon ./cmd/proton-sync-daemon
    cd "$SCRIPT_DIR"
    echo "Go daemon built successfully"
else
    echo "Warning: Go not found, skipping Go daemon build"
fi

# Clean previous build
rm -rf "$SCRIPT_DIR/build"
mkdir -p "$BUILD_DIR/DEBIAN"
mkdir -p "$BUILD_DIR/usr/bin"
mkdir -p "$BUILD_DIR/usr/share/proton-drive-gtk"
mkdir -p "$BUILD_DIR/usr/share/proton-drive-gtk/bin"
mkdir -p "$BUILD_DIR/usr/share/applications"
mkdir -p "$BUILD_DIR/usr/share/doc/${PACKAGE}"
mkdir -p "$BUILD_DIR/usr/share/nautilus-python/extensions"

# Create icon directories
SIZES="16x16 22x22 24x24 32x32 48x48"
for size in $SIZES; do
    mkdir -p "$BUILD_DIR/usr/share/icons/hicolor/$size/emblems"
done
mkdir -p "$BUILD_DIR/usr/share/icons/hicolor/scalable/emblems"

# Copy source files (including subdirectories)
cp "$SCRIPT_DIR/src/"*.py "$BUILD_DIR/usr/share/proton-drive-gtk/"

# Copy daemon module
mkdir -p "$BUILD_DIR/usr/share/proton-drive-gtk/daemon"
cp "$SCRIPT_DIR/src/daemon/"*.py "$BUILD_DIR/usr/share/proton-drive-gtk/daemon/"

# Copy utils module if it exists
if [ -d "$SCRIPT_DIR/src/utils" ]; then
    mkdir -p "$BUILD_DIR/usr/share/proton-drive-gtk/utils"
    cp "$SCRIPT_DIR/src/utils/"*.py "$BUILD_DIR/usr/share/proton-drive-gtk/utils/" 2>/dev/null || true
fi

# Copy Go daemon binary if it was built
if [ -f "$SCRIPT_DIR/go-daemon/bin/proton-sync-daemon" ]; then
    cp "$SCRIPT_DIR/go-daemon/bin/proton-sync-daemon" "$BUILD_DIR/usr/share/proton-drive-gtk/bin/"
    chmod 755 "$BUILD_DIR/usr/share/proton-drive-gtk/bin/proton-sync-daemon"
    echo "Go daemon binary included in package"
fi

# Copy Nautilus extension
cp "$SCRIPT_DIR/nautilus/proton_drive_nautilus.py" "$BUILD_DIR/usr/share/nautilus-python/extensions/"

# Copy emblem icons
for size in $SIZES; do
    if [ -d "$SCRIPT_DIR/assets/icons/emblems/$size" ]; then
        cp "$SCRIPT_DIR/assets/icons/emblems/$size/"*.png "$BUILD_DIR/usr/share/icons/hicolor/$size/emblems/" 2>/dev/null || true
    fi
done
cp "$SCRIPT_DIR/assets/icons/emblems/scalable/"*.svg "$BUILD_DIR/usr/share/icons/hicolor/scalable/emblems/" 2>/dev/null || true

# Copy and make executable the launcher
cp "$SCRIPT_DIR/bin/proton-drive-gtk" "$BUILD_DIR/usr/bin/"
chmod 755 "$BUILD_DIR/usr/bin/proton-drive-gtk"

# Copy desktop entry
cp "$SCRIPT_DIR/assets/proton-drive-gtk.desktop.in" "$BUILD_DIR/usr/share/applications/proton-drive-gtk.desktop"

# Copy documentation
cp "$SCRIPT_DIR/README.md" "$BUILD_DIR/usr/share/doc/${PACKAGE}/"
cp "$SCRIPT_DIR/LICENSE" "$BUILD_DIR/usr/share/doc/${PACKAGE}/copyright"

# Create control file
cat > "$BUILD_DIR/DEBIAN/control" << EOF
Package: ${PACKAGE}
Version: ${VERSION}
Section: net
Priority: optional
Architecture: ${ARCH}
Depends: python3, python3-gi, gir1.2-ayatanaappindicator3-0.1, rclone (>= 1.61), python3-pip
Recommends: python3-nautilus
Maintainer: Achraf Soltani <achraf.soltani@gmail.com>
Homepage: https://github.com/achrafsoltani/proton-drive-gtk
Description: GTK system tray application for Proton Drive
 A lightweight GTK system tray application for Proton Drive,
 powered by rclone. Features include:
  - Mount/unmount Proton Drive with one click
  - Transfer rate display during sync
  - Pause/resume sync
  - Auto-mount on startup
  - Settings dialog for configuration
  - Nautilus sync status emblems (synced/syncing/pending/error/cloud/downloading)
  - Smart Sync: Download Now and Free Up Space context menu actions
EOF

# Create postinst script
cat > "$BUILD_DIR/DEBIAN/postinst" << 'EOF'
#!/bin/bash
set -e

# Update desktop database
if command -v update-desktop-database &> /dev/null; then
    update-desktop-database -q /usr/share/applications/ 2>/dev/null || true
fi

# Update icon cache for emblems
if command -v gtk-update-icon-cache &> /dev/null; then
    gtk-update-icon-cache -f -t /usr/share/icons/hicolor 2>/dev/null || true
fi

# Install inotify for real-time file watching
if ! python3 -c "import inotify" 2>/dev/null; then
    echo "Installing inotify for real-time file watching..."
    pip3 install --quiet inotify 2>/dev/null || true
fi

echo ""
echo "Proton Drive GTK installed successfully!"
echo ""
echo "Before using, configure rclone with your Proton account:"
echo "  rclone config"
echo ""
echo "Then run: proton-drive-gtk"
echo ""
echo "To start automatically on login, enable it in Preferences."
echo ""
echo "Note: For Nautilus sync emblems, install python3-nautilus and restart Nautilus:"
echo "  sudo apt install python3-nautilus"
echo "  nautilus -q"
echo ""
EOF
chmod 755 "$BUILD_DIR/DEBIAN/postinst"

# Build the package
dpkg-deb --build "$BUILD_DIR"

# Move to dist directory
mkdir -p "$SCRIPT_DIR/dist"
mv "$SCRIPT_DIR/build/${PACKAGE}_${VERSION}_${ARCH}.deb" "$SCRIPT_DIR/dist/"

echo ""
echo "=== Build complete ==="
echo "Package: $SCRIPT_DIR/dist/${PACKAGE}_${VERSION}_${ARCH}.deb"
echo ""
echo "Install with: sudo dpkg -i dist/${PACKAGE}_${VERSION}_${ARCH}.deb"
