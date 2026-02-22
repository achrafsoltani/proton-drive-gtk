"""Nautilus extension for Proton Drive sync status emblems and context menu.

This extension displays sync status emblems on files within the Proton Drive
mount folder and provides context menu actions for managing file sync state.

Emblem mapping:
- emblem-proton-synced: File is fully synced/cached locally (green checkmark)
- emblem-proton-syncing: File is currently uploading (purple circular arrows)
- emblem-proton-pending: File is queued for upload (orange clock)
- emblem-proton-error: Upload failed after retries (red X)
- emblem-proton-cloud: File exists on cloud only, not cached locally (blue cloud)
- emblem-proton-downloading: File is currently being downloaded (blue down arrow)

Context menu actions:
- "Download Now": Download cloud-only files to local cache
- "Free Up Space": Remove local cache, keeping file on cloud only
"""

import shutil
import socket
import subprocess
import time
import threading
from pathlib import Path
from typing import List
from urllib.parse import unquote

from gi.repository import GObject, Nautilus, GLib

# Socket path - must match the server
SOCKET_PATH = Path.home() / ".cache" / "proton-drive-gtk" / "nautilus.sock"

# Default mount path and remote name
DEFAULT_MOUNT_PATH = Path.home() / "ProtonDrive"
REMOTE_NAME = "protondrive"

# VFS cache directory
VFS_CACHE_DIR = Path.home() / ".cache" / "rclone" / "vfs" / REMOTE_NAME

# Cache settings
CACHE_TTL = 5.0  # seconds
SOCKET_TIMEOUT = 1.0  # seconds

# Emblem names mapping
EMBLEM_MAP = {
    "synced": "emblem-proton-synced",
    "syncing": "emblem-proton-syncing",
    "pending": "emblem-proton-pending",
    "error": "emblem-proton-error",
    "cloud": "emblem-proton-cloud",
    "downloading": "emblem-proton-downloading",
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

    def clear(self, path=None):
        with self._lock:
            if path:
                self._cache.pop(path, None)
            else:
                self._cache.clear()


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


def _get_relative_path(file_path):
    """Get the path relative to mount point."""
    mount_path = _get_mount_path()
    if mount_path is None:
        return None
    try:
        return file_path.relative_to(mount_path)
    except ValueError:
        return None


def _get_cache_path(file_path):
    """Get the VFS cache path for a file."""
    rel_path = _get_relative_path(Path(file_path))
    if rel_path is None:
        return None
    return VFS_CACHE_DIR / rel_path


def _is_file_cached(file_path):
    """Check if a file is cached locally."""
    cache_path = _get_cache_path(file_path)
    if cache_path is None:
        return False
    return cache_path.exists()


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
    cached = _cache.get(path_str)
    if cached is not None and cached != "synced":
        return cached

    # Query socket
    status = _query_socket(path_str)
    if status:
        if status != "synced":
            _cache.set(path_str, status)
        return status

    return None


def _download_file(file_path):
    """Download a file using cat command to trigger VFS caching."""
    try:
        path = Path(file_path)
        if path.is_dir():
            # For directories, download all contents
            for item in path.rglob('*'):
                if item.is_file():
                    subprocess.run(
                        ['cat', str(item)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=600
                    )
        else:
            # For files, use cat to read and trigger download
            subprocess.run(
                ['cat', str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=600
            )
        return True
    except Exception:
        pass
    return False


def _free_up_space(file_path):
    """Remove a file from the local VFS cache."""
    try:
        cache_path = _get_cache_path(file_path)
        if cache_path is None:
            return False

        if cache_path.exists():
            if cache_path.is_file():
                cache_path.unlink()
            elif cache_path.is_dir():
                shutil.rmtree(cache_path)

            # Also remove metadata
            meta_dir = Path.home() / ".cache" / "rclone" / "vfsMeta" / REMOTE_NAME
            rel_path = _get_relative_path(Path(file_path))
            if rel_path:
                meta_path = meta_dir / rel_path
                if meta_path.exists():
                    if meta_path.is_file():
                        meta_path.unlink()
                    elif meta_path.is_dir():
                        shutil.rmtree(meta_path)

            _cache.clear(str(file_path))
            return True
    except Exception:
        pass
    return False


class ProtonDriveInfoProvider(GObject.GObject, Nautilus.InfoProvider):
    """Nautilus extension providing sync status emblems for Proton Drive files."""

    def __init__(self):
        super().__init__()

    def update_file_info(self, file: Nautilus.FileInfo) -> None:
        """Called by Nautilus for each visible file."""
        uri = file.get_uri()
        if file.get_uri_scheme() != "file":
            return

        path_str = unquote(uri[7:])
        file_path = Path(path_str).resolve()

        if not _is_proton_drive_file(file_path):
            return

        status = _get_file_status(file_path)

        if status and status in EMBLEM_MAP:
            file.add_emblem(EMBLEM_MAP[status])


class ProtonDriveMenuProvider(GObject.GObject, Nautilus.MenuProvider):
    """Nautilus extension providing context menu actions for Proton Drive files."""

    def __init__(self):
        super().__init__()

    def _get_file_paths(self, files: List[Nautilus.FileInfo]) -> List[Path]:
        """Extract file paths from Nautilus FileInfo objects."""
        paths = []
        for file in files:
            if file.get_uri_scheme() != "file":
                continue
            uri = file.get_uri()
            path_str = unquote(uri[7:])
            file_path = Path(path_str).resolve()
            if _is_proton_drive_file(file_path):
                paths.append(file_path)
        return paths

    def _on_download_activate(self, menu, files):
        """Handle 'Download Now' menu action."""
        paths = self._get_file_paths(files)
        for path in paths:
            threading.Thread(
                target=_download_file,
                args=(str(path),),
                daemon=True
            ).start()

    def _on_free_space_activate(self, menu, files):
        """Handle 'Free Up Space' menu action."""
        paths = self._get_file_paths(files)
        for path in paths:
            _free_up_space(str(path))

    def get_file_items(
        self,
        files: List[Nautilus.FileInfo],
    ) -> List[Nautilus.MenuItem]:
        """Get context menu items for selected files."""
        paths = self._get_file_paths(files)
        if not paths:
            return []

        items = []

        # Check statuses of selected files
        has_cloud_only = False
        has_cached = False

        for path in paths:
            if _is_file_cached(str(path)):
                has_cached = True
            else:
                has_cloud_only = True

        # Add "Download Now" if any files are cloud-only
        if has_cloud_only:
            item = Nautilus.MenuItem(
                name="ProtonDrive::DownloadNow",
                label="Download Now",
                tip="Download selected files to local cache",
            )
            item.connect("activate", self._on_download_activate, files)
            items.append(item)

        # Add "Free Up Space" if any files are cached
        if has_cached:
            item = Nautilus.MenuItem(
                name="ProtonDrive::FreeUpSpace",
                label="Free Up Space",
                tip="Remove local cache, keep file on cloud",
            )
            item.connect("activate", self._on_free_space_activate, files)
            items.append(item)

        return items

    def get_background_items(
        self,
        current_folder: Nautilus.FileInfo,
    ) -> List[Nautilus.MenuItem]:
        """Get context menu items for folder background (not used)."""
        return []
