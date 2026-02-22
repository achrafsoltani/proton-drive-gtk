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

## Requirements

- Python 3.10+
- GTK 3 / libappindicator
- rclone 1.61+ (with protondrive support)

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

```bash
rclone config
# Select 'n' for new remote
# Name it 'protondrive'
# Select 'protondrive' as storage type
# Enter your Proton credentials
```

### 4. Clone and install

```bash
git clone https://github.com/AchrafSoltani/proton-drive-gtk.git
cd proton-drive-gtk
./install.sh
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
├── install.sh        # Installation script
├── run.sh            # Run script (backgrounds by default)
├── src/
│   ├── main.py       # Entry point
│   ├── tray.py       # System tray implementation
│   ├── rclone.py     # rclone wrapper
│   └── config.py     # Settings management
├── assets/
│   └── proton-drive-gtk.desktop
└── tests/
```

## Configuration

Settings are stored in `~/.config/proton-drive-gtk/config.json`:

- **mount_path** - Where to mount Proton Drive (default: `~/ProtonDrive`)
- **auto_mount** - Mount automatically on startup
- **vfs_cache_mode** - rclone cache mode (default: `full`)
- **show_notifications** - Enable desktop notifications

## License

MIT
