#!/bin/bash
# Build .deb package for proton-drive-gtk

set -e

VERSION="1.0.0"
PACKAGE="proton-drive-gtk"
ARCH="all"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/${PACKAGE}_${VERSION}_${ARCH}"

echo "=== Building ${PACKAGE} ${VERSION} ==="

# Clean previous build
rm -rf "$SCRIPT_DIR/build"
mkdir -p "$BUILD_DIR/DEBIAN"
mkdir -p "$BUILD_DIR/usr/bin"
mkdir -p "$BUILD_DIR/usr/share/proton-drive-gtk"
mkdir -p "$BUILD_DIR/usr/share/applications"
mkdir -p "$BUILD_DIR/usr/share/doc/${PACKAGE}"

# Copy source files
cp "$SCRIPT_DIR/src/"*.py "$BUILD_DIR/usr/share/proton-drive-gtk/"

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
Depends: python3, python3-gi, gir1.2-ayatanaappindicator3-0.1, rclone (>= 1.61)
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
EOF

# Create postinst script
cat > "$BUILD_DIR/DEBIAN/postinst" << 'EOF'
#!/bin/bash
set -e

# Update desktop database
if command -v update-desktop-database &> /dev/null; then
    update-desktop-database -q /usr/share/applications/ 2>/dev/null || true
fi

echo ""
echo "Proton Drive GTK installed successfully!"
echo ""
echo "Before using, configure rclone with your Proton account:"
echo "  rclone config"
echo ""
echo "Then run: proton-drive-gtk"
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
