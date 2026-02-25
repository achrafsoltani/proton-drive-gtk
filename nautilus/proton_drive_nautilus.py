"""Nautilus extension for Proton Drive sync status emblems and context menu.

This extension displays sync status emblems on files within the Proton Drive
mount folder and provides context menu actions for managing file sync state.

Production-ready with:
- Robust socket communication
- Proper error handling
- Avoids slow VFS operations
- Reliable progress tracking

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
from typing import List, Optional
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
SOCKET_TIMEOUT = 2.0  # seconds (reduced for responsiveness)

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
    """Thread-safe cache for file statuses with TTL."""

    def __init__(self):
        self._cache = {}
        self._lock = threading.Lock()

    def get(self, path: str) -> Optional[str]:
        with self._lock:
            entry = self._cache.get(path)
            if entry:
                status, timestamp = entry
                if time.time() - timestamp < CACHE_TTL:
                    return status
                # Expired, remove it
                del self._cache[path]
            return None

    def set(self, path: str, status: str) -> None:
        with self._lock:
            self._cache[path] = (status, time.time())

    def clear(self, path: str = None) -> None:
        with self._lock:
            if path:
                self._cache.pop(path, None)
            else:
                self._cache.clear()


# Global cache instance
_cache = StatusCache()
_mount_path = None


def _get_mount_path() -> Optional[Path]:
    """Get the Proton Drive mount path."""
    global _mount_path
    if _mount_path is None:
        if DEFAULT_MOUNT_PATH.exists():
            _mount_path = DEFAULT_MOUNT_PATH.resolve()
    return _mount_path


def _is_proton_drive_file(file_path: Path) -> bool:
    """Check if a file is within the Proton Drive mount."""
    mount_path = _get_mount_path()
    if mount_path is None:
        return False
    try:
        file_path.relative_to(mount_path)
        return True
    except ValueError:
        return False


def _get_relative_path(file_path: Path) -> Optional[Path]:
    """Get the path relative to mount point."""
    mount_path = _get_mount_path()
    if mount_path is None:
        return None
    try:
        return file_path.relative_to(mount_path)
    except ValueError:
        return None


def _get_cache_path(file_path: str) -> Optional[Path]:
    """Get the VFS cache path for a file."""
    rel_path = _get_relative_path(Path(file_path))
    if rel_path is None:
        return None
    return VFS_CACHE_DIR / rel_path


def _is_file_cached(file_path: str) -> bool:
    """Check if a file has local cache, or if a folder has ANY cached files.

    For files: returns True if the file is in cache.
    For folders: returns True if ANY files are cached (partial cache counts).
    Uses local cache directory (fast) instead of VFS mount.
    """
    cache_path = _get_cache_path(file_path)
    if cache_path is None:
        return False

    if not cache_path.exists():
        return False

    if cache_path.is_file():
        return True

    if cache_path.is_dir():
        try:
            for item in cache_path.rglob('*'):
                if item.is_file():
                    return True
            return False
        except (OSError, IOError):
            return False

    return False


def _has_uncached_content(file_path: str) -> bool:
    """Check if a file/folder has content that is NOT cached.

    For files: returns True if the file is not in cache.
    For folders: returns True if the folder exists (assumes it may have uncached content).
    This is a fast approximation - we don't list the VFS mount.
    """
    path = Path(file_path)
    cache_path = _get_cache_path(file_path)

    if cache_path is None:
        return False

    # For files, check if cache exists
    if path.is_file():
        return not cache_path.exists()

    # For folders, we can't easily know without listing VFS (slow)
    # So we use a heuristic: if the folder exists on mount, assume it may have uncached content
    # unless we know the cache is complete (which we can't easily determine)
    if path.is_dir():
        # If no cache at all, definitely has uncached content
        if not cache_path.exists():
            return True
        # If cache exists, it might be partial - assume yes for folders
        # This means folders will show both buttons, which is safer
        return True

    return False


def _send_socket_command(command: str, timeout: float = SOCKET_TIMEOUT) -> Optional[str]:
    """Send a command to the tray socket and return response."""
    if not SOCKET_PATH.exists():
        return None

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(SOCKET_PATH))
        sock.sendall(command.encode("utf-8"))

        response = b""
        while not response.endswith(b"done\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
            if len(response) > 65536:  # Max 64KB response
                break

        sock.close()
        return response.decode("utf-8", errors="replace") if response else None
    except (socket.error, OSError, socket.timeout):
        return None


def _query_socket(file_path: str) -> Optional[str]:
    """Query the socket server for file status."""
    request = f"STATUS\npath\t{file_path}\ndone\n"
    response = _send_socket_command(request)

    if response:
        lines = response.strip().split("\n")
        if lines and lines[0] == "ok":
            for line in lines[1:]:
                if line.startswith("status\t"):
                    status = line[7:].strip()
                    # Validate status
                    if status in EMBLEM_MAP or status == "unknown":
                        return status
    return None


def _get_file_status(file_path: Path) -> Optional[str]:
    """Get status for a file, using cache when possible."""
    path_str = str(file_path)

    # Check cache first (but not for synced status - always verify)
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


def _notify_download_start(file_path: str, total_bytes: int = 0, file_count: int = 1) -> bool:
    """Notify tray that a download is starting."""
    command = f"DOWNLOAD_START\npath\t{file_path}\nbytes\t{total_bytes}\nfiles\t{file_count}\ndone\n"
    response = _send_socket_command(command)
    return response is not None and "ok" in response


def _notify_download_complete(file_path: str) -> bool:
    """Notify tray that a download is complete."""
    command = f"DOWNLOAD_COMPLETE\npath\t{file_path}\ndone\n"
    response = _send_socket_command(command)
    return response is not None and "ok" in response


def _download_file(file_path: str) -> bool:
    """Download a file/folder using rclone copy to the VFS cache.

    Uses rclone copy command which is more reliable than cat for bulk downloads.
    Copies directly to the VFS cache directory to populate the cache.
    """
    path = Path(file_path)
    rel_path = _get_relative_path(path)

    if rel_path is None:
        return False

    try:
        # Get size estimate (for single files only, to avoid slow VFS listing)
        total_bytes = 0
        if path.is_file():
            try:
                total_bytes = path.stat().st_size
            except (OSError, IOError):
                pass

        # Notify tray that download is starting
        _notify_download_start(file_path, total_bytes, 1 if path.is_file() else 0)

        # Build rclone copy command
        # Source: remote path (e.g., protondrive:/path/to/file)
        # Dest: VFS cache directory
        remote_path = f"{REMOTE_NAME}:/{rel_path}"
        cache_dest = VFS_CACHE_DIR / rel_path.parent if path.is_file() else VFS_CACHE_DIR / rel_path.parent

        # Ensure destination directory exists
        cache_dest.mkdir(parents=True, exist_ok=True)

        # For files, copy the specific file
        # For folders, copy the entire folder
        if path.is_file():
            dest_path = str(VFS_CACHE_DIR / rel_path.parent)
        else:
            dest_path = str(VFS_CACHE_DIR / rel_path)
            Path(dest_path).mkdir(parents=True, exist_ok=True)

        # Run rclone copy with progress
        result = subprocess.run(
            [
                'rclone', 'copy',
                remote_path,
                dest_path,
                '--transfers=4',  # Parallel transfers
                '--checkers=8',
                '-v'
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3600  # 1 hour timeout for large folders
        )

        success = result.returncode == 0

        # Notify completion
        _notify_download_complete(file_path)
        return success

    except subprocess.TimeoutExpired:
        _notify_download_complete(file_path)
        return False
    except Exception:
        _notify_download_complete(file_path)
        return False


def _free_up_space(file_path: str) -> bool:
    """Remove a file/folder from the local VFS cache."""
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

            # Notify tray that cache was cleared (so it updates status)
            _send_socket_command(f"CACHE_CLEARED\npath\t{file_path}\ndone\n")
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
            # Run download in background thread
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
            path_str = str(path)
            if _is_file_cached(path_str):
                has_cached = True
            if _has_uncached_content(path_str):
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
