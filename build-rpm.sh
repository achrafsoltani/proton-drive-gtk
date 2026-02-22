#!/bin/bash
# Build .rpm package for proton-drive-gtk

set -e

VERSION="1.0.0"
PACKAGE="proton-drive-gtk"
RELEASE="1"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="$SCRIPT_DIR/build/rpm"

echo "=== Building ${PACKAGE} ${VERSION} RPM ==="

# Check for rpmbuild
if ! command -v rpmbuild &> /dev/null; then
    echo "Error: rpmbuild not found. Install with:"
    echo "  Fedora/RHEL: sudo dnf install rpm-build"
    echo "  openSUSE: sudo zypper install rpm-build"
    exit 1
fi

# Clean previous build
rm -rf "$BUILD_ROOT"
mkdir -p "$BUILD_ROOT"/{BUILD,RPMS,SOURCES,SPECS,SRPMS}

# Create tarball
TARBALL_DIR="${PACKAGE}-${VERSION}"
mkdir -p "$BUILD_ROOT/SOURCES/$TARBALL_DIR"
cp -r "$SCRIPT_DIR/src" "$BUILD_ROOT/SOURCES/$TARBALL_DIR/"
cp -r "$SCRIPT_DIR/bin" "$BUILD_ROOT/SOURCES/$TARBALL_DIR/"
cp -r "$SCRIPT_DIR/assets" "$BUILD_ROOT/SOURCES/$TARBALL_DIR/"
cp "$SCRIPT_DIR/README.md" "$BUILD_ROOT/SOURCES/$TARBALL_DIR/"
cp "$SCRIPT_DIR/LICENSE" "$BUILD_ROOT/SOURCES/$TARBALL_DIR/"

cd "$BUILD_ROOT/SOURCES"
tar czf "${PACKAGE}-${VERSION}.tar.gz" "$TARBALL_DIR"
rm -rf "$TARBALL_DIR"
cd "$SCRIPT_DIR"

# Create spec file
cat > "$BUILD_ROOT/SPECS/${PACKAGE}.spec" << EOF
Name:           ${PACKAGE}
Version:        ${VERSION}
Release:        ${RELEASE}%{?dist}
Summary:        GTK system tray application for Proton Drive

License:        MIT
URL:            https://github.com/achrafsoltani/proton-drive-gtk
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  python3-devel

Requires:       python3
Requires:       python3-gobject
Requires:       libappindicator-gtk3
Requires:       rclone >= 1.61

%description
A lightweight GTK system tray application for Proton Drive,
powered by rclone. Features include:
- Mount/unmount Proton Drive with one click
- Transfer rate display during sync
- Pause/resume sync
- Auto-mount on startup
- Settings dialog for configuration

%prep
%setup -q

%install
mkdir -p %{buildroot}%{_bindir}
mkdir -p %{buildroot}%{_datadir}/%{name}
mkdir -p %{buildroot}%{_datadir}/applications
mkdir -p %{buildroot}%{_docdir}/%{name}

install -m 755 bin/proton-drive-gtk %{buildroot}%{_bindir}/
install -m 644 src/*.py %{buildroot}%{_datadir}/%{name}/
install -m 644 assets/proton-drive-gtk.desktop.in %{buildroot}%{_datadir}/applications/proton-drive-gtk.desktop
install -m 644 README.md %{buildroot}%{_docdir}/%{name}/
install -m 644 LICENSE %{buildroot}%{_docdir}/%{name}/

%files
%license LICENSE
%doc README.md
%{_bindir}/proton-drive-gtk
%{_datadir}/%{name}/
%{_datadir}/applications/proton-drive-gtk.desktop

%changelog
* Sat Feb 22 2026 Achraf Soltani <achraf.soltani@gmail.com> - 1.0.0-1
- Initial release
- System tray with mount/unmount controls
- Transfer rate display
- Pause/resume sync
- Settings dialog
- RC authentication for security
EOF

# Build RPM
rpmbuild --define "_topdir $BUILD_ROOT" -bb "$BUILD_ROOT/SPECS/${PACKAGE}.spec"

# Move to dist directory
mkdir -p "$SCRIPT_DIR/dist"
find "$BUILD_ROOT/RPMS" -name "*.rpm" -exec cp {} "$SCRIPT_DIR/dist/" \;

echo ""
echo "=== Build complete ==="
echo "Package: $SCRIPT_DIR/dist/${PACKAGE}-${VERSION}-${RELEASE}.noarch.rpm"
echo ""
echo "Install with: sudo dnf install dist/${PACKAGE}-${VERSION}-${RELEASE}.noarch.rpm"
