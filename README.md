# Proton Drive GTK

A lightweight GTK system tray application for Proton Drive, powered by rclone.

## Features

- System tray icon with sync status indicator
- Mount/unmount Proton Drive with one click
- Transfer rate display during sync
- Pause/resume sync
- Auto-mount on startup (optional)
- Open mount folder in file manager
- Settings dialog for configuration
- **Nautilus sync status emblems** - Dropbox-style file sync indicators

## Nautilus Sync Status Emblems

Files in your Proton Drive folder display sync status emblems (like Dropbox):

| Emblem | Colour | Status |
|--------|--------|--------|
| Checkmark | Green | Synced - file is cached locally |
| Arrows | Purple | Syncing - file is currently uploading |
| Clock | Orange | Pending - file is queued for upload |
| X | Red | Error - upload failed after retries |
| Cloud | Blue | Cloud-only - file exists on cloud, not cached locally |
| Down Arrow | Blue | Downloading - file is currently being downloaded |

**Note:** Press F5 in Nautilus to refresh emblems after status changes.

### Context Menu Actions

Right-click on files in your Proton Drive folder for these options:

| Action | Description |
|--------|-------------|
| **Download Now** | Download cloud-only files to local cache |
| **Free Up Space** | Remove local cache, keeping file on cloud only |

### Enable Nautilus extension

```bash
# Install python3-nautilus (if not already installed)
sudo apt install python3-nautilus  # Debian/Ubuntu
sudo dnf install nautilus-python   # Fedora

# Copy extension to user directory
sudo cp /usr/share/proton-drive-gtk/nautilus/proton_drive_nautilus.py /usr/share/nautilus-python/extensions/

# Restart Nautilus
nautilus -q
```

## Requirements

- Python 3.10+
- GTK 3 / libappindicator
- rclone 1.61+ (with protondrive support)
- python3-nautilus (optional, for sync emblems)

## Installation

### 1. Install system dependencies

```bash
sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1
```

### 2. Install/update rclone (needs 1.61+ for Proton Drive)

```bash
curl https://rclone.org/install.sh | sudo bash
```

### 3. Configure rclone with Proton Drive

**Important:** You must have logged into Proton Drive via the web browser at least once to generate encryption keys before using rclone.

```bash
rclone config
```

Follow the interactive prompts:

```
No remotes found, make a new one?
n) New remote
s) Set configuration password
q) Quit config
n/s/q> n

Enter name for new remote.
name> protondrive

Option Storage.
Type of storage to configure.
Choose a number from below, or type in your own value.
...
XX / Proton Drive
   \ (protondrive)
...
Storage> protondrive

Option username.
The username of your proton account
Enter a value.
username> your.email@proton.me

Option password.
The password of your proton account.
y) Yes, type in my own password
g) Generate random password
n) No, leave this optional password blank
y/g/n> y
Enter the password: [hidden]
Confirm the password: [hidden]

Option 2fa.
The 2FA code (if 2FA is enabled on the account)
Enter a value. Press Enter to leave empty.
2fa> [enter code or leave blank]

Edit advanced config?
y) Yes
n) No (default)
y/n> n

Configuration complete.
Options:
- type: protondrive
- username: your.email@proton.me
- password: *** ENCRYPTED ***

Keep this "protondrive" remote?
y) Yes this is OK (default)
e) Edit this remote
d) Delete this remote
y/e/d> y
```

Verify the configuration:

```bash
rclone lsd protondrive:
```

### 4. Install

**Option A: Install from .deb package (Debian/Ubuntu)**

```bash
# Download latest release
wget https://github.com/AchrafSoltani/proton-drive-gtk/releases/latest/download/proton-drive-gtk_1.0.0_all.deb

# Install
sudo dpkg -i proton-drive-gtk_1.0.0_all.deb
sudo apt-get install -f  # Install dependencies if needed
```

**Option B: Install from .rpm package (Fedora/RHEL/openSUSE)**

```bash
# Download latest release
wget https://github.com/AchrafSoltani/proton-drive-gtk/releases/latest/download/proton-drive-gtk-1.0.0-1.noarch.rpm

# Install (Fedora/RHEL)
sudo dnf install proton-drive-gtk-1.0.0-1.noarch.rpm

# Install (openSUSE)
sudo zypper install proton-drive-gtk-1.0.0-1.noarch.rpm
```

**Option C: Install from source**

```bash
git clone https://github.com/AchrafSoltani/proton-drive-gtk.git
cd proton-drive-gtk
./install.sh
```

**Option D: Build packages from source**

```bash
git clone https://github.com/AchrafSoltani/proton-drive-gtk.git
cd proton-drive-gtk

# Build .deb (Debian/Ubuntu)
./build-deb.sh
sudo dpkg -i dist/proton-drive-gtk_1.0.0_all.deb

# Build .rpm (Fedora/RHEL) - requires rpm-build
./build-rpm.sh
sudo dnf install dist/proton-drive-gtk-1.0.0-1.noarch.rpm
```

## Usage

### Run the application

```bash
./run.sh          # Runs in background
./run.sh --fg     # Runs in foreground (for debugging)
```

Or search "Proton Drive" in your application menu.

### Tray menu options

- **Mount / Unmount** - Toggle Proton Drive mount
- **Pause / Resume Sync** - Pause or resume transfers
- **Open Folder** - Open mount folder in file manager
- **Settings** - Configure mount path and options
- **Restart** - Restart the application
- **Quit** - Unmount and exit

## Project Structure

```
proton-drive-gtk/
├── install.sh            # Installation script
├── run.sh                # Run script (backgrounds by default)
├── build-deb.sh          # Build .deb package
├── build-rpm.sh          # Build .rpm package
├── src/
│   ├── main.py           # Entry point
│   ├── tray.py           # System tray implementation
│   ├── rclone.py         # rclone wrapper
│   ├── config.py         # Settings management
│   └── nautilus_server.py # Socket server for Nautilus extension
├── nautilus/
│   └── proton_drive_nautilus.py  # Nautilus extension
├── assets/
│   ├── proton-drive-gtk.desktop
│   └── icons/
│       └── emblems/      # Sync status emblem icons
└── bin/
    └── proton-drive-gtk  # Launcher script
```

## Configuration

Settings are stored in `~/.config/proton-drive-gtk/config.json`:

- **mount_path** - Where to mount Proton Drive (default: `~/ProtonDrive`)
- **auto_mount** - Mount automatically on startup
- **vfs_cache_mode** - rclone cache mode (default: `full`)
- **show_notifications** - Enable desktop notifications

## Security Considerations

### rclone password storage
rclone stores your Proton password in `~/.config/rclone/rclone.conf` using "obscured" encoding, which is **not encryption**. Anyone with read access to this file can decode your password.

**Mitigations:**
- Ensure proper file permissions: `chmod 600 ~/.config/rclone/rclone.conf`
- Consider using rclone's `--password-command` for external secret management
- Use full-disk encryption on your system

### RC interface authentication
The rclone remote control interface is protected with randomly-generated per-session credentials. This prevents other local processes from controlling your mount.

### VFS cache
Cached files are stored in `~/.cache/rclone/vfs/` unencrypted. Local processes with your user permissions can read these files.

**Mitigations:**
- Use `vfs-cache-mode=off` or `minimal` if caching sensitive files is a concern
- Ensure ~/.cache has proper permissions
- Use full-disk encryption

### Recommendations
1. Enable 2FA on your Proton account
2. Use full-disk encryption (LUKS)
3. Keep your system updated
4. Review rclone config file permissions

## License

MIT
