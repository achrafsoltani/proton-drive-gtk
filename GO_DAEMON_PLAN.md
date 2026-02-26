# Go Sync Daemon Implementation Plan

## Executive Summary

Replace the Python sync daemon with a performant Go implementation. Current issues:
- **Memory**: Python uses 4.4GB during large syncs
- **Database locking**: Thread-local SQLite connections cause contention
- **GIL**: Python's threading model limits true concurrency

## Architecture

```
┌─────────────────────┐
│  Python Tray (GTK)  │  ← UI only, lightweight
└──────────┬──────────┘
           │ Unix Socket (daemon.sock)
┌──────────▼──────────┐
│   Go Sync Daemon    │
│  ├── watcher/       │  ← inotify file watching
│  ├── sync/          │  ← Sync engine
│  ├── db/            │  ← SQLite (single writer)
│  ├── rclone/        │  ← rclone wrapper
│  └── ipc/           │  ← Unix socket server
└──────────┬──────────┘
           │
┌──────────▼──────────┐
│    Proton Drive     │
└─────────────────────┘
```

## Project Structure

```
proton-drive-gtk/
├── src/                          # Existing Python (tray, UI)
├── go-daemon/                    # New Go sync daemon
│   ├── cmd/
│   │   └── proton-sync-daemon/   # Main entry point
│   ├── internal/
│   │   ├── config/               # Configuration loading
│   │   ├── daemon/               # Main daemon orchestration
│   │   ├── db/                   # SQLite state database
│   │   ├── ipc/                  # Unix socket server
│   │   ├── rclone/               # rclone command wrapper
│   │   ├── sync/                 # Sync engine logic
│   │   └── watcher/              # inotify file watcher
│   ├── pkg/
│   │   └── protocol/             # Shared IPC protocol types
│   ├── go.mod
│   ├── go.sum
│   └── Makefile
├── debian/                       # Updated packaging
└── build-deb.sh                  # Updated build script
```

## Key Components

### 1. Database Layer (`internal/db/`)

```go
type StateDB struct {
    db        *sql.DB
    writeMu   sync.Mutex    // Serialise all writes
}

func Open(dbPath string) (*StateDB, error) {
    db, err := sql.Open("sqlite", dbPath+"?_journal_mode=WAL&_busy_timeout=30000")
    db.SetMaxOpenConns(1)  // Single writer eliminates contention
    return &StateDB{db: db}, nil
}
```

### 2. File Watcher (`internal/watcher/`)

```go
type Watcher struct {
    watcher  *fsnotify.Watcher
    events   chan FileEvent  // Bounded channel (1000 events)
}
```

- Back-pressure prevents memory accumulation
- Debouncing coalesces rapid changes

### 3. Sync Engine (`internal/sync/`)

```go
// Stream remote listing - no large slices
func (e *Engine) StreamRemoteList(ctx context.Context, handler func(RemoteFile) error) error {
    cmd := exec.CommandContext(ctx, "rclone", "lsjson", e.remoteName+":")
    decoder := json.NewDecoder(stdout)
    for decoder.More() {
        var rf RemoteFile
        decoder.Decode(&rf)
        handler(rf)  // Process one file at a time
    }
}
```

### 4. IPC Protocol

Same protocol as existing Nautilus server:

```
# Request
STATS
done

# Response
ok
status	running
total_files	1234
synced_files	1200
pending_upload	10
done
```

## Python Tray Client

```python
# src/daemon_client.py
class DaemonClient:
    def __init__(self, socket_path: Path = SOCKET_PATH):
        self.socket_path = socket_path

    def get_stats(self) -> DaemonStats:
        resp = self._send_request(["STATS"])
        return DaemonStats(...)

    def force_sync(self) -> bool:
        return self._send_request(["SYNC"])

    def pause(self) -> bool:
        return self._send_request(["PAUSE"])

    def resume(self) -> bool:
        return self._send_request(["RESUME"])
```

## Migration Strategy

### Phase 1: Build Go Daemon (Week 1-2)
- Core sync functionality
- New socket path (`daemon.sock`)
- Feature flag in Python tray

### Phase 2: Integration (Week 3)
- `daemon_client.py` for IPC
- Modify `bisync_tray.py` to use Go daemon
- Test both daemons in parallel

### Phase 3: Default Go (Week 4)
- Set `use_go_daemon = True` as default
- Update packaging

### Phase 4: Cleanup (Week 5)
- Remove Python daemon code
- Documentation update

## Expected Performance

| Metric | Python | Go |
|--------|--------|-----|
| Memory (idle) | 150 MB | 15 MB |
| Memory (syncing) | 4.4 GB | 50 MB |
| DB lock timeouts | Frequent | None |
| Startup time | 2 sec | 100 ms |
| Event latency | 500 ms | 10 ms |

## Build Integration

### Makefile

```makefile
build:
	CGO_ENABLED=0 go build -o bin/proton-sync-daemon ./cmd/proton-sync-daemon

linux-amd64:
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o bin/proton-sync-daemon-linux-amd64
```

### Updated build-deb.sh

```bash
# Build Go daemon
cd go-daemon && make linux-amd64 && cd ..

# Copy binary
cp go-daemon/bin/proton-sync-daemon-linux-amd64 \
   "$BUILD_DIR/usr/share/proton-drive-gtk/bin/proton-sync-daemon"
```

## Dependencies

Go modules:
- `modernc.org/sqlite` - Pure Go SQLite (no CGO)
- `github.com/fsnotify/fsnotify` - File watcher
- `log/slog` - Structured logging (stdlib)

## Implementation Order

1. `internal/config/` - Config loading
2. `internal/db/` - SQLite with single-writer pattern
3. `internal/watcher/` - inotify with bounded channels
4. `internal/rclone/` - Command wrapper
5. `internal/sync/` - Sync engine with streaming
6. `internal/ipc/` - Unix socket server
7. `internal/daemon/` - Orchestration
8. `cmd/proton-sync-daemon/` - Main entry
9. `src/daemon_client.py` - Python client
10. Integration with `bisync_tray.py`
