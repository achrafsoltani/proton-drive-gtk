# Proton Drive GTK - Bisync Architecture

## Overview

A reliable Proton Drive sync client for Linux, using rclone bisync for true bidirectional synchronization instead of VFS mount.

**Future-proofing:** When Proton releases SDK authentication support, this can be migrated to a native implementation.

## Current vs New Architecture

### Current (VFS Mount) - Problems
```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   Nautilus      │────▶│  rclone mount   │────▶│  Proton Drive   │
│   Extension     │     │  (VFS cache)    │     │  API            │
└─────────────────┘     └─────────────────┘     └─────────────────┘
                              │
                              ▼
                        On-demand caching
                        (unreliable, slow)
```

**Issues:**
- VFS is a network filesystem, not a sync solution
- On-demand caching is slow and unreliable
- Can't determine true sync state
- Downloads via `cat` don't work reliably for folders

### New (Bisync) - Reliable
```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   Local Folder  │◀───▶│  Sync Daemon    │◀───▶│  Proton Drive   │
│   ~/ProtonDrive │     │  (rclone bisync)│     │  (via rclone)   │
└─────────────────┘     └─────────────────┘     └─────────────────┘
        │                       │
        ▼                       ▼
   Real files              State DB
   (always available)      (tracks sync)
```

**Benefits:**
- Files are real local files (always accessible)
- True sync state tracking
- Conflict detection and resolution
- Works offline, syncs when online
- Reliable progress tracking

## Components

### 1. Sync Daemon (`sync_daemon.py`)
Core sync engine that runs in background.

```python
class SyncDaemon:
    """Main sync daemon using rclone bisync."""

    def __init__(self, config):
        self.local_path = config.sync_folder  # ~/ProtonDrive
        self.remote = config.remote_name      # protondrive:
        self.state_db = StateDatabase(config.db_path)
        self.watcher = FileWatcher(self.local_path)

    def start(self):
        """Start the sync daemon."""
        # Initial sync
        self.full_sync()

        # Watch for local changes
        self.watcher.start(self.on_local_change)

        # Periodic remote check
        self.schedule_remote_check(interval=60)

    def full_sync(self):
        """Run full bisync."""
        subprocess.run([
            'rclone', 'bisync',
            self.local_path,
            f'{self.remote}:/',
            '--resilient',
            '--recover',
            '--conflict-resolve', 'newer',
            '--verbose'
        ])
        self.update_state_db()

    def on_local_change(self, path, event_type):
        """Handle local file changes."""
        self.state_db.mark_pending(path)
        self.schedule_sync(debounce=5)  # Wait 5s for more changes
```

### 2. State Database (`state_db.py`)
SQLite database tracking sync state of every file.

```sql
CREATE TABLE files (
    path TEXT PRIMARY KEY,
    local_mtime REAL,
    remote_mtime REAL,
    local_size INTEGER,
    remote_size INTEGER,
    local_hash TEXT,
    remote_hash TEXT,
    status TEXT,  -- 'synced', 'pending_upload', 'pending_download', 'conflict', 'error'
    last_sync REAL,
    error_message TEXT
);

CREATE TABLE sync_history (
    id INTEGER PRIMARY KEY,
    timestamp REAL,
    action TEXT,  -- 'upload', 'download', 'delete', 'conflict'
    path TEXT,
    result TEXT
);
```

**File States:**
| Status | Icon | Meaning |
|--------|------|---------|
| `synced` | ✓ Green | File matches remote |
| `pending_upload` | ↑ Orange | Local changes not yet uploaded |
| `pending_download` | ↓ Blue | Remote changes not yet downloaded |
| `syncing` | ⟳ Purple | Currently transferring |
| `conflict` | ⚠ Yellow | Both sides changed |
| `error` | ✗ Red | Sync failed |

### 3. File Watcher (`file_watcher.py`)
Uses inotify to detect local changes in real-time.

```python
import inotify.adapters

class FileWatcher:
    """Watch local folder for changes using inotify."""

    EVENTS = (
        'IN_CREATE', 'IN_DELETE', 'IN_MODIFY',
        'IN_MOVED_FROM', 'IN_MOVED_TO', 'IN_CLOSE_WRITE'
    )

    def __init__(self, path):
        self.path = path
        self.inotify = inotify.adapters.InotifyTree(path)

    def start(self, callback):
        """Start watching for changes."""
        for event in self.inotify.event_gen(yield_nones=False):
            (_, event_types, path, filename) = event
            if any(e in self.EVENTS for e in event_types):
                full_path = os.path.join(path, filename)
                callback(full_path, event_types)
```

### 4. Tray Application (`tray.py`)
System tray for status and controls.

```python
class ProtonDriveTray:
    """System tray application."""

    def __init__(self, daemon):
        self.daemon = daemon
        self.build_menu()

    def update_status(self):
        stats = self.daemon.get_sync_stats()

        if stats.syncing:
            self.set_icon(ICON_SYNCING)
            self.status_item.set_label(
                f"Syncing: {stats.current_file}\n"
                f"{stats.uploaded}/{stats.total} files"
            )
        elif stats.pending > 0:
            self.set_icon(ICON_PENDING)
            self.status_item.set_label(f"Pending: {stats.pending} files")
        else:
            self.set_icon(ICON_SYNCED)
            self.status_item.set_label("All files synced")
```

### 5. Nautilus Extension (`nautilus_extension.py`)
Shows sync status emblems on files.

```python
class ProtonDriveInfoProvider(Nautilus.InfoProvider):
    """Show sync status emblems."""

    def __init__(self):
        self.db = StateDatabase()  # Read-only connection

    def update_file_info(self, file):
        path = self.get_path(file)
        status = self.db.get_status(path)

        if status:
            file.add_emblem(EMBLEM_MAP[status])
```

### 6. Selective Sync (`selective_sync.py`)
Choose which folders to sync locally.

```python
class SelectiveSync:
    """Manage which folders are synced."""

    def __init__(self, config_path):
        self.config = self.load_config(config_path)

    def get_filter_rules(self):
        """Generate rclone filter rules."""
        rules = []
        for folder, enabled in self.config.folders.items():
            if enabled:
                rules.append(f"+ {folder}/**")
            else:
                rules.append(f"- {folder}/**")
        return rules

    def set_folder_sync(self, folder, enabled):
        """Enable/disable sync for a folder."""
        self.config.folders[folder] = enabled
        self.save_config()
        # Trigger resync if needed
```

## Sync Flow

### Initial Setup
```
1. User configures rclone remote (existing)
2. User selects local sync folder
3. Run initial bisync --resync
4. Populate state database
5. Start file watcher
6. Start periodic remote checks
```

### Normal Operation
```
Local Change Detected:
  1. inotify triggers callback
  2. Mark file as 'pending_upload' in DB
  3. Debounce for 5 seconds
  4. Run incremental bisync
  5. Update DB with results
  6. Notify Nautilus to refresh

Remote Change Detected (periodic check):
  1. Run rclone lsf to check remote
  2. Compare with state DB
  3. Mark changed files as 'pending_download'
  4. Run incremental bisync
  5. Update DB with results
  6. Notify Nautilus to refresh
```

### Conflict Resolution
```
Both sides changed:
  1. Detect during bisync
  2. Mark as 'conflict' in DB
  3. Apply resolution strategy:
     - 'newer' wins (default)
     - Keep both with suffix
     - Manual resolution
  4. Notify user via tray
```

## Directory Structure

```
proton-drive-gtk/
├── src/
│   ├── main.py              # Entry point
│   ├── daemon/
│   │   ├── sync_daemon.py   # Core sync engine
│   │   ├── file_watcher.py  # inotify watcher
│   │   ├── state_db.py      # SQLite state tracking
│   │   └── selective_sync.py
│   ├── tray/
│   │   ├── tray.py          # System tray
│   │   ├── settings.py      # Settings dialog
│   │   └── icons.py         # Icon management
│   ├── nautilus/
│   │   ├── extension.py     # Nautilus extension
│   │   └── socket_server.py # IPC with extension
│   └── utils/
│       ├── rclone.py        # rclone wrapper
│       ├── config.py        # Configuration
│       └── logging.py       # Logging setup
├── nautilus/
│   └── proton_drive_nautilus.py  # Installed extension
├── assets/
│   └── icons/
└── tests/
```

## Configuration

```json
{
  "remote_name": "protondrive",
  "sync_folder": "~/ProtonDrive",
  "selective_sync": {
    "enabled": true,
    "folders": {
      "Documents": true,
      "Photos": true,
      "Backups": false
    }
  },
  "sync_interval": 60,
  "conflict_resolution": "newer",
  "notifications": true,
  "start_on_login": true
}
```

## Implementation Plan

### Phase 1: Core Sync (Week 1)
- [ ] Sync daemon with bisync
- [ ] State database
- [ ] Basic tray with status

### Phase 2: File Watching (Week 2)
- [ ] inotify integration
- [ ] Debounced sync triggers
- [ ] Progress tracking

### Phase 3: Nautilus Integration (Week 3)
- [ ] Extension with DB-based status
- [ ] Context menu actions
- [ ] Real-time emblem updates

### Phase 4: Selective Sync (Week 4)
- [ ] Folder selection UI
- [ ] Filter rule generation
- [ ] Partial sync support

### Phase 5: Polish (Week 5)
- [ ] Conflict resolution UI
- [ ] Notifications
- [ ] Error handling
- [ ] Testing & packaging

## Future: Native SDK Migration

When Proton releases SDK authentication:

```python
# Future native implementation
from proton_drive_sdk import ProtonDrive, Auth

class NativeSyncDaemon:
    def __init__(self):
        self.auth = Auth()
        self.drive = ProtonDrive(self.auth.session)

    def sync(self):
        # Direct API calls instead of rclone
        changes = self.drive.get_changes(since=self.last_sync)
        for change in changes:
            self.apply_change(change)
```

The architecture is designed to make this migration straightforward:
- State database remains the same
- File watcher remains the same
- Only the sync engine changes
- Tray and Nautilus extension stay compatible
