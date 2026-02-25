"""Nautilus integration server for sync status emblems.

Production-ready implementation with:
- Robust socket communication with size limits
- Copy-on-write cache updates to avoid race conditions
- Fast operations that avoid slow VFS mount traversal
- Proper error handling and logging
"""

import logging
import os
import socket
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Callable, Tuple
from dataclasses import dataclass, field

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('nautilus_server')


class FileStatus(Enum):
    """Sync status for files."""
    SYNCED = "synced"
    SYNCING = "syncing"
    PENDING = "pending"
    ERROR = "error"
    CLOUD = "cloud"
    DOWNLOADING = "downloading"
    UNKNOWN = "unknown"


@dataclass
class QueuedFile:
    """Information about a file in the sync queue."""
    path: str
    status: FileStatus
    tries: int = 0
    error: Optional[str] = None


@dataclass
class ActiveDownload:
    """Information about an active download triggered by Nautilus."""
    path: str
    total_bytes: int
    cached_bytes: int = 0
    file_count: int = 0
    cached_count: int = 0
    start_time: float = field(default_factory=time.time)
    last_update: float = field(default_factory=time.time)

    @property
    def progress(self) -> float:
        """Return progress as percentage (0-100)."""
        if self.total_bytes <= 0:
            return 0.0
        return min(100.0, (self.cached_bytes / self.total_bytes) * 100)

    @property
    def is_complete(self) -> bool:
        """Check if download is complete."""
        if self.total_bytes <= 0:
            # For unknown size, consider complete if no updates for 30s
            return (time.time() - self.last_update) > 30
        return self.cached_bytes >= self.total_bytes

    @property
    def is_stale(self) -> bool:
        """Check if download tracking should be removed (timeout or stuck)."""
        # Remove after 30 minutes or if no progress for 5 minutes
        elapsed = time.time() - self.start_time
        since_update = time.time() - self.last_update
        return elapsed > 1800 or since_update > 300


class DownloadTracker:
    """Tracks active downloads triggered from Nautilus context menu."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self._downloads: Dict[str, ActiveDownload] = {}
        self._completed: Dict[str, float] = {}  # path -> completion timestamp
        self._lock = threading.Lock()
        self.COMPLETED_TTL = 3600  # Keep completed status for 1 hour

    def start_download(self, path: str, total_bytes: int, file_count: int = 1) -> None:
        """Register a new download."""
        with self._lock:
            self._downloads[path] = ActiveDownload(
                path=path,
                total_bytes=total_bytes,
                file_count=file_count
            )
            logger.info(f"Download started: {path} ({total_bytes} bytes, {file_count} files)")

    def complete_download(self, path: str) -> None:
        """Mark a download as complete and remember it was synced."""
        with self._lock:
            if path in self._downloads:
                del self._downloads[path]
            # Remember this path was fully downloaded
            self._completed[path] = time.time()
            # Clean up old completed entries
            cutoff = time.time() - self.COMPLETED_TTL
            self._completed = {p: t for p, t in self._completed.items() if t > cutoff}
            logger.info(f"Download completed: {path}")

    def is_recently_completed(self, path: str) -> bool:
        """Check if a path (or its parent) was recently fully downloaded."""
        normalised = str(Path(path).resolve())
        with self._lock:
            cutoff = time.time() - self.COMPLETED_TTL
            for completed_path, timestamp in self._completed.items():
                if timestamp < cutoff:
                    continue
                # Check if the queried path is the completed path or inside it
                if normalised == completed_path or normalised.startswith(completed_path + "/"):
                    return True
            return False

    def clear_completed(self, path: str) -> None:
        """Clear the completed status for a path (e.g., when freeing space)."""
        with self._lock:
            self._completed.pop(path, None)
            # Also remove any children
            prefix = path + "/"
            self._completed = {p: t for p, t in self._completed.items()
                            if not p.startswith(prefix)}

    def update_progress(self, mount_path: Path, remote_name: str) -> None:
        """Update progress for all active downloads by checking cache directory."""
        # Build new state outside lock
        updates = {}
        to_remove = []

        with self._lock:
            downloads_snapshot = dict(self._downloads)

        for path, download in downloads_snapshot.items():
            try:
                file_path = Path(path)
                relative = file_path.relative_to(mount_path)
                cache_path = self.cache_dir / remote_name / relative
            except ValueError:
                to_remove.append(path)
                continue

            # Calculate cached bytes from LOCAL cache (fast)
            cached_bytes = 0
            cached_count = 0
            try:
                if cache_path.exists():
                    if cache_path.is_file():
                        cached_bytes = cache_path.stat().st_size
                        cached_count = 1
                    elif cache_path.is_dir():
                        # Walk local cache directory (fast, not VFS)
                        for f in cache_path.rglob('*'):
                            if f.is_file():
                                try:
                                    cached_bytes += f.stat().st_size
                                    cached_count += 1
                                except OSError:
                                    pass
            except OSError as e:
                logger.debug(f"Error checking cache for {path}: {e}")

            updates[path] = (cached_bytes, cached_count)

        # Apply updates with lock
        with self._lock:
            for path, (cached_bytes, cached_count) in updates.items():
                if path in self._downloads:
                    download = self._downloads[path]
                    if cached_bytes != download.cached_bytes:
                        download.cached_bytes = cached_bytes
                        download.cached_count = cached_count
                        download.last_update = time.time()

            # Remove stale/completed downloads
            for path in list(self._downloads.keys()):
                download = self._downloads[path]
                if download.is_complete or download.is_stale:
                    del self._downloads[path]
                    logger.info(f"Download tracking removed: {path}")

            for path in to_remove:
                self._downloads.pop(path, None)

    def get_active_downloads(self) -> Dict[str, ActiveDownload]:
        """Get all active downloads."""
        with self._lock:
            return dict(self._downloads)

    def is_downloading(self, path: str) -> bool:
        """Check if a path or any of its children are being downloaded."""
        normalised = str(Path(path).resolve())
        with self._lock:
            for download_path in self._downloads:
                if normalised == download_path or normalised.startswith(download_path + "/"):
                    return True
                if download_path.startswith(normalised + "/"):
                    return True
            return False

    def get_total_progress(self) -> Optional[Tuple[int, int, int]]:
        """Get aggregated download progress.

        Returns: (total_bytes, cached_bytes, download_count) or None if no downloads.
        """
        with self._lock:
            if not self._downloads:
                return None
            total = sum(d.total_bytes for d in self._downloads.values())
            cached = sum(d.cached_bytes for d in self._downloads.values())
            count = len(self._downloads)
            return (total, cached, count)


class SyncStatusCache:
    """Cache for file sync statuses, parsed from rclone vfs/queue and core/stats.

    Uses copy-on-write pattern to avoid race conditions during updates.
    """

    VFS_CACHE_DIR = Path.home() / ".cache" / "rclone" / "vfs"

    def __init__(self, mount_path: str, remote_name: str = "protondrive"):
        self.mount_path = Path(mount_path).resolve()
        self.remote_name = remote_name
        self._upload_cache: Dict[str, QueuedFile] = {}
        self._downloading: set = set()
        self._lock = threading.RLock()  # Reentrant lock for nested calls
        self._last_update = 0.0

    def update_from_vfs_queue(self, queue_data: Optional[dict]) -> None:
        """Update cache from rclone vfs/queue response using copy-on-write."""
        # Build new cache outside lock
        new_cache: Dict[str, QueuedFile] = {}

        if queue_data:
            for item in queue_data.get("queue", []):
                path = self._normalize_path(item.get("name", ""))
                if path:
                    tries = item.get("tries", 0)
                    uploading = item.get("uploading", False)

                    if tries > 3:
                        status = FileStatus.ERROR
                    elif uploading:
                        status = FileStatus.SYNCING
                    else:
                        status = FileStatus.PENDING

                    new_cache[path] = QueuedFile(
                        path=path,
                        status=status,
                        tries=tries,
                        error=None
                    )

        # Atomic swap
        with self._lock:
            self._upload_cache = new_cache
            self._last_update = time.time()

    def update_from_core_stats(self, stats_data: Optional[dict]) -> None:
        """Update downloading files from rclone core/stats response."""
        new_downloading: set = set()

        if stats_data:
            for item in stats_data.get("transferring", []) or []:
                src_fs = item.get("srcFs", "")
                if src_fs and src_fs.rstrip(":") == self.remote_name:
                    name = item.get("name", "")
                    if name:
                        path = self._normalize_path(name)
                        if path:
                            new_downloading.add(path)

        # Atomic swap
        with self._lock:
            self._downloading = new_downloading

    def _get_cache_path(self, file_path: str) -> Optional[Path]:
        """Get the VFS cache path for a file/folder."""
        try:
            file_path_obj = Path(file_path)
            relative_path = file_path_obj.relative_to(self.mount_path)
            return self.VFS_CACHE_DIR / self.remote_name / relative_path
        except (ValueError, OSError):
            return None

    def _is_file_cached(self, file_path: str) -> bool:
        """Check if a file is cached/synced locally.

        For bisync mode: file exists locally in mount_path = synced
        For VFS mode: file exists in VFS cache = synced
        """
        file_path_obj = Path(file_path)

        # In bisync mode, files are directly in mount_path
        # If the file exists locally, it's synced
        if file_path_obj.exists() and file_path_obj.is_file():
            return True

        # Also check VFS cache for mount mode compatibility
        cache_path = self._get_cache_path(file_path)
        if cache_path is not None and cache_path.exists():
            return True

        return False

    def _get_folder_status(self, folder_path: str) -> FileStatus:
        """Get the sync status for a folder.

        For bisync mode: if folder exists locally with content, it's synced.
        For VFS mode: uses VFS cache heuristics.
        """
        folder = Path(folder_path)
        folder_str = str(folder.resolve())

        # Quick checks with lock
        with self._lock:
            # Check if any tracked file in this folder is downloading
            for path in self._downloading:
                if path.startswith(folder_str + "/"):
                    return FileStatus.DOWNLOADING

            # Check upload queue
            for path, qf in self._upload_cache.items():
                if path.startswith(folder_str + "/"):
                    if qf.status in (FileStatus.SYNCING, FileStatus.PENDING):
                        return qf.status
                    if qf.status == FileStatus.ERROR:
                        return FileStatus.ERROR

        # In bisync mode, if folder exists locally, it's synced
        if folder.exists() and folder.is_dir():
            return FileStatus.SYNCED

        return FileStatus.CLOUD

    def _normalize_path(self, path: str) -> str:
        """Normalize a file path to absolute mount path."""
        if not path:
            return ""
        clean_path = path.lstrip("/")
        return str((self.mount_path / clean_path).resolve())

    def get_status(self, file_path: str) -> FileStatus:
        """Get the sync status for a file or folder."""
        normalised = str(Path(file_path).resolve())
        file_path_obj = Path(normalised)

        # Check if it's within the mount path
        try:
            file_path_obj.relative_to(self.mount_path)
        except ValueError:
            return FileStatus.UNKNOWN

        # Handle directories separately
        if file_path_obj.is_dir():
            return self._get_folder_status(normalised)

        # File handling with lock for quick checks
        with self._lock:
            # 1. Check if currently downloading (rclone transfer)
            if normalised in self._downloading:
                return FileStatus.DOWNLOADING

            # 2. Check upload queue (syncing/pending/error)
            if normalised in self._upload_cache:
                return self._upload_cache[normalised].status

        # 3. Check if file was recently modified (likely pending upload)
        try:
            if file_path_obj.exists():
                mtime = file_path_obj.stat().st_mtime
                age = time.time() - mtime
                if age < 30:
                    return FileStatus.PENDING
        except (OSError, IOError):
            pass

        # 4. Check if file is cached locally
        if self._is_file_cached(normalised):
            return FileStatus.SYNCED

        # 5. File exists on mount but not cached = cloud-only
        return FileStatus.CLOUD

    def get_all_statuses(self) -> Dict[str, FileStatus]:
        """Get all file statuses in the cache (uploads and downloads)."""
        with self._lock:
            result = {path: qf.status for path, qf in self._upload_cache.items()}
            for path in self._downloading:
                result[path] = FileStatus.DOWNLOADING
            return result


class NautilusSocketServer:
    """Unix socket server for Nautilus extension communication.

    Protocol (line-based text):
    Request:
        STATUS
        path\t/path/to/file
        done

        DOWNLOAD_START
        path\t/path/to/file
        bytes\t12345
        files\t3
        done

        DOWNLOAD_COMPLETE
        path\t/path/to/file
        done

    Response:
        ok
        status\tsynced|syncing|pending|error|unknown
        done
    """

    SOCKET_DIR = Path.home() / ".cache" / "proton-drive-gtk"
    SOCKET_NAME = "nautilus.sock"
    VFS_CACHE_DIR = Path.home() / ".cache" / "rclone" / "vfs"
    MAX_REQUEST_SIZE = 65536  # 64 KB limit

    def __init__(self, status_cache: SyncStatusCache, mount_path: str, remote_name: str):
        self.status_cache = status_cache
        self.mount_path = Path(mount_path).resolve()
        self.remote_name = remote_name
        self.download_tracker = DownloadTracker(self.VFS_CACHE_DIR)
        self._socket: Optional[socket.socket] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._socket_path = self.SOCKET_DIR / self.SOCKET_NAME

    @property
    def socket_path(self) -> Path:
        """Get the socket path."""
        return self._socket_path

    def start(self) -> bool:
        """Start the socket server."""
        if self._running:
            return True

        try:
            self.SOCKET_DIR.mkdir(parents=True, exist_ok=True)

            # Remove existing socket file safely
            try:
                self._socket_path.unlink()
            except FileNotFoundError:
                pass
            except PermissionError:
                logger.error(f"Socket {self._socket_path} is in use by another process")
                return False

            self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._socket.bind(str(self._socket_path))
            self._socket.listen(10)
            self._socket.settimeout(1.0)

            self._running = True
            self._thread = threading.Thread(target=self._serve, daemon=True)
            self._thread.start()

            logger.info(f"Nautilus socket server started at {self._socket_path}")
            return True

        except Exception as e:
            logger.error(f"Failed to start Nautilus socket server: {e}")
            return False

    def stop(self) -> None:
        """Stop the socket server."""
        self._running = False

        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

        try:
            self._socket_path.unlink()
        except Exception:
            pass

        logger.info("Nautilus socket server stopped")

    def _serve(self) -> None:
        """Main server loop."""
        while self._running:
            try:
                client, _ = self._socket.accept()
                threading.Thread(
                    target=self._handle_client,
                    args=(client,),
                    daemon=True
                ).start()
            except socket.timeout:
                continue
            except OSError as e:
                if self._running:
                    logger.error(f"Socket accept error: {e}")
                break
            except Exception as e:
                if self._running:
                    logger.error(f"Socket server error: {e}")
                break

    def _handle_client(self, client: socket.socket) -> None:
        """Handle a client connection with proper bounds checking."""
        try:
            client.settimeout(5.0)
            data = b""
            start_time = time.time()

            while not data.endswith(b"done\n"):
                if len(data) > self.MAX_REQUEST_SIZE:
                    raise ValueError("Request exceeds maximum size")
                if time.time() - start_time > 5.0:
                    raise TimeoutError("Client read timeout")

                try:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                except socket.timeout:
                    break

            if data:
                lines = data.decode("utf-8", errors="replace").strip().split("\n")
                response = self._process_request(lines)
                client.sendall(response.encode("utf-8"))

        except Exception as e:
            logger.debug(f"Client error: {e}")
            try:
                client.sendall(f"error\nmessage\t{str(e)}\ndone\n".encode("utf-8"))
            except Exception:
                pass
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _process_request(self, lines: list) -> str:
        """Process a request and return response."""
        if not lines:
            return "error\nmessage\tEmpty request\ndone\n"

        command = lines[0].strip().upper()

        if command == "STATUS":
            file_path = None
            for line in lines[1:]:
                if line.startswith("path\t"):
                    file_path = line[5:].strip()
                    break

            if not file_path:
                return "error\nmessage\tNo path provided\ndone\n"

            # Check if actively downloading
            if self.download_tracker.is_downloading(file_path):
                return "ok\nstatus\tdownloading\ndone\n"

            # Check if recently completed (fully synced)
            if self.download_tracker.is_recently_completed(file_path):
                return "ok\nstatus\tsynced\ndone\n"

            status = self.status_cache.get_status(file_path)
            return f"ok\nstatus\t{status.value}\ndone\n"

        elif command == "PING":
            return "ok\npong\ndone\n"

        elif command == "LIST":
            statuses = self.status_cache.get_all_statuses()
            response = "ok\n"
            for path, status in statuses.items():
                response += f"file\t{path}\t{status.value}\n"
            response += "done\n"
            return response

        elif command == "DOWNLOAD_START":
            file_path = None
            total_bytes = 0
            file_count = 1
            for line in lines[1:]:
                if line.startswith("path\t"):
                    file_path = line[5:].strip()
                elif line.startswith("bytes\t"):
                    try:
                        total_bytes = int(line[6:].strip())
                    except ValueError:
                        pass
                elif line.startswith("files\t"):
                    try:
                        file_count = int(line[6:].strip())
                    except ValueError:
                        pass

            if not file_path:
                return "error\nmessage\tNo path provided\ndone\n"

            self.download_tracker.start_download(file_path, total_bytes, file_count)
            return "ok\ndone\n"

        elif command == "DOWNLOAD_COMPLETE":
            file_path = None
            for line in lines[1:]:
                if line.startswith("path\t"):
                    file_path = line[5:].strip()
                    break

            if file_path:
                self.download_tracker.complete_download(file_path)
            return "ok\ndone\n"

        elif command == "DOWNLOAD_PROGRESS":
            progress = self.download_tracker.get_total_progress()
            if progress:
                total, cached, count = progress
                return f"ok\ntotal\t{total}\ncached\t{cached}\ncount\t{count}\ndone\n"
            return "ok\ncount\t0\ndone\n"

        elif command == "CACHE_CLEARED":
            # Called when user frees up space - clear completed status
            file_path = None
            for line in lines[1:]:
                if line.startswith("path\t"):
                    file_path = line[5:].strip()
                    break
            if file_path:
                self.download_tracker.clear_completed(file_path)
            return "ok\ndone\n"

        else:
            return f"error\nmessage\tUnknown command: {command}\ndone\n"


class NautilusIntegration:
    """Main integration class combining cache and server."""

    def __init__(
        self,
        mount_path: str,
        remote_name: str,
        get_vfs_queue_func: Callable[[], Optional[dict]],
        get_core_stats_func: Callable[[], Optional[dict]]
    ):
        self.mount_path = Path(mount_path).resolve()
        self.remote_name = remote_name
        self._get_vfs_queue = get_vfs_queue_func
        self._get_core_stats = get_core_stats_func
        self.cache = SyncStatusCache(mount_path, remote_name)
        self.server = NautilusSocketServer(self.cache, mount_path, remote_name)
        self._update_thread: Optional[threading.Thread] = None
        self._running = False
        self._idle_count = 0

    def start(self) -> bool:
        """Start the integration (server + cache updates)."""
        if not self.server.start():
            return False

        self._running = True
        self._update_thread = threading.Thread(target=self._update_loop, daemon=True)
        self._update_thread.start()

        return True

    def stop(self) -> None:
        """Stop the integration."""
        self._running = False

        if self._update_thread and self._update_thread.is_alive():
            self._update_thread.join(timeout=2.0)

        self.server.stop()

    def update_cache(self) -> None:
        """Manually trigger a cache update."""
        try:
            queue_data = self._get_vfs_queue()
            self.cache.update_from_vfs_queue(queue_data)
        except Exception as e:
            logger.error(f"Error updating VFS queue: {e}")

        try:
            stats_data = self._get_core_stats()
            self.cache.update_from_core_stats(stats_data)
        except Exception as e:
            logger.error(f"Error updating core stats: {e}")

    def _update_loop(self) -> None:
        """Background loop to update cache with adaptive interval."""
        while self._running:
            try:
                self.update_cache()
                self.server.download_tracker.update_progress(
                    self.mount_path, self.remote_name
                )

                # Adaptive update interval based on activity
                has_activity = (
                    self.cache.get_all_statuses() or
                    self.server.download_tracker.get_total_progress()
                )
                if has_activity:
                    self._idle_count = 0
                    interval = 2  # Active: 2s
                else:
                    self._idle_count += 1
                    interval = min(10, 2 + self._idle_count)  # Idle: 2-10s

            except Exception as e:
                logger.error(f"Cache update error: {e}")
                interval = 5

            # Sleep with early exit check
            for _ in range(int(interval * 10)):
                if not self._running:
                    break
                time.sleep(0.1)

    def get_download_progress(self) -> Optional[Tuple[int, int, int]]:
        """Get current download progress.

        Returns: (total_bytes, cached_bytes, download_count) or None if no downloads.
        """
        return self.server.download_tracker.get_total_progress()
