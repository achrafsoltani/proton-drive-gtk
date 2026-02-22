"""Nautilus extension for Proton Drive sync status emblems.

This extension displays sync status emblems on files within the Proton Drive
mount folder. It communicates with the proton-drive-gtk application via a
Unix socket to query file statuses.

Emblem mapping:
- emblem-proton-synced: File is fully synced (green checkmark)
- emblem-proton-syncing: File is currently uploading (circular arrows)
- emblem-proton-pending: File is queued for upload (clock)
- emblem-proton-error: Upload failed after retries (red X)
"""

import os
import socket
import time
import threading
from pathlib import Path
from urllib.parse import unquote

# Import Nautilus - don't require version, let it auto-detect
from gi.repository import GObject, Nautilus, GLib

print("Initializing proton-drive-gtk Nautilus extension")

# Socket path - must match the server
SOCKET_PATH = Path.home() / ".cache" / "proton-drive-gtk" / "nautilus.sock"

# Default mount path
DEFAULT_MOUNT_PATH = Path.home() / "ProtonDrive"

# Cache settings
CACHE_TTL = 5.0  # seconds
SOCKET_TIMEOUT = 1.0  # seconds

# Emblem names mapping
EMBLEM_MAP = {
    "synced": "emblem-proton-synced",
    "syncing": "emblem-proton-syncing",
    "pending": "emblem-proton-pending",
    "error": "emblem-proton-error",
}


class StatusCache:
    """Simple cache for file statuses."""

    def __init__(self):
        self._cache = {}
        self._lock = threading.Lock()

    def get(self, path):
        with self._lock:
            entry = self._cache.get(path)
            if entry:
                status, timestamp = entry
                if time.time() - timestamp < CACHE_TTL:
                    return status
            return None

    def set(self, path, status):
        with self._lock:
            self._cache[path] = (status, time.time())


# Global cache instance
_cache = StatusCache()
_mount_path = None


def _get_mount_path():
    """Get the Proton Drive mount path."""
    global _mount_path
    if _mount_path is None:
        if DEFAULT_MOUNT_PATH.exists():
            _mount_path = DEFAULT_MOUNT_PATH.resolve()
    return _mount_path


def _is_proton_drive_file(file_path):
    """Check if a file is within the Proton Drive mount."""
    mount_path = _get_mount_path()
    if mount_path is None:
        return False
    try:
        file_path.relative_to(mount_path)
        return True
    except ValueError:
        return False


def _query_socket(file_path):
    """Query the socket server for file status."""
    if not SOCKET_PATH.exists():
        return None

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(SOCKET_TIMEOUT)
        sock.connect(str(SOCKET_PATH))

        request = f"STATUS\npath\t{file_path}\ndone\n"
        sock.sendall(request.encode("utf-8"))

        response = b""
        while not response.endswith(b"done\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk

        sock.close()

        lines = response.decode("utf-8", errors="replace").strip().split("\n")
        if lines and lines[0] == "ok":
            for line in lines[1:]:
                if line.startswith("status\t"):
                    return line[7:].strip()
        return None

    except (socket.error, OSError, socket.timeout):
        return None


def _get_file_status(file_path):
    """Get status for a file, using cache when possible."""
    path_str = str(file_path)

    # Check cache - but only for non-synced statuses
    # (synced files may become pending/syncing, so always recheck)
    cached = _cache.get(path_str)
    if cached is not None and cached != "synced":
        return cached

    # Query socket
    status = _query_socket(path_str)
    if status:
        # Only cache non-synced statuses to catch state changes
        if status != "synced":
            _cache.set(path_str, status)
        return status

    return None


class ProtonDriveInfoProvider(GObject.GObject, Nautilus.InfoProvider):
    """Nautilus extension providing sync status emblems for Proton Drive files."""

    def __init__(self):
        super().__init__()
        self._tracked_files = {}  # path -> (file_info, last_status)
        self._refresh_timeout = None
        # Start periodic refresh checker
        GLib.timeout_add_seconds(3, self._check_for_changes)

    def _check_for_changes(self):
        """Periodically check for status changes (placeholder for future auto-refresh)."""
        # Auto-refresh not currently working with Nautilus 4.0
        # Keeping timer infrastructure for potential future improvements
        return True

    def update_file_info(self, file: Nautilus.FileInfo) -> None:
        """Called by Nautilus for each visible file."""
        uri = file.get_uri()
        if file.get_uri_scheme() != "file":
            return

        # Decode URI to path
        path_str = unquote(uri[7:])
        file_path = Path(path_str).resolve()

        # Check if it's a Proton Drive file
        if not _is_proton_drive_file(file_path):
            return

        # Get status
        status = _get_file_status(file_path)

        # Track this file for auto-refresh (future use)
        path_key = str(file_path)
        if status and status != "synced":
            self._tracked_files[path_key] = status
        elif path_key in self._tracked_files:
            del self._tracked_files[path_key]

        # Apply emblem
        if status and status in EMBLEM_MAP:
            file.add_emblem(EMBLEM_MAP[status])
