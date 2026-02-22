"""Nautilus integration server for sync status emblems."""

import os
import socket
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Callable
from dataclasses import dataclass


class FileStatus(Enum):
    """Sync status for files."""
    SYNCED = "synced"
    SYNCING = "syncing"
    PENDING = "pending"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass
class QueuedFile:
    """Information about a file in the sync queue."""
    path: str
    status: FileStatus
    tries: int = 0
    error: Optional[str] = None


class SyncStatusCache:
    """Cache for file sync statuses, parsed from rclone vfs/queue."""

    def __init__(self, mount_path: str):
        self.mount_path = Path(mount_path).resolve()
        self._cache: Dict[str, QueuedFile] = {}
        self._lock = threading.Lock()
        self._last_update = 0.0

    def update_from_vfs_queue(self, queue_data: Optional[dict]) -> None:
        """Update cache from rclone vfs/queue response.

        The vfs/queue response has the structure:
        {
            "queue": [
                {
                    "name": "filename.txt",
                    "size": 1234,
                    "tries": 0,
                    "uploading": true,
                    "delay": 10,
                    "expiry": 123.456
                }
            ]
        }
        """
        with self._lock:
            self._cache.clear()
            self._last_update = time.time()

            if not queue_data:
                return

            # Process queue items
            for item in queue_data.get("queue", []):
                path = self._normalize_path(item.get("name", ""))
                if path:
                    tries = item.get("tries", 0)
                    uploading = item.get("uploading", False)

                    # Determine status based on state
                    if tries > 3:
                        status = FileStatus.ERROR
                    elif uploading:
                        status = FileStatus.SYNCING
                    else:
                        status = FileStatus.PENDING

                    self._cache[path] = QueuedFile(
                        path=path,
                        status=status,
                        tries=tries,
                        error=None
                    )

    def _normalize_path(self, path: str) -> str:
        """Normalize a file path to absolute mount path."""
        if not path:
            return ""

        # Always treat the path as relative to mount_path
        # Strip leading slash if present
        clean_path = path.lstrip("/")
        return str((self.mount_path / clean_path).resolve())

    def get_status(self, file_path: str) -> FileStatus:
        """Get the sync status for a file."""
        with self._lock:
            # Normalise the path for comparison
            normalised = str(Path(file_path).resolve())

            if normalised in self._cache:
                return self._cache[normalised].status

            # Check if it's within the mount path
            try:
                Path(normalised).relative_to(self.mount_path)
            except ValueError:
                # Not in mount path
                return FileStatus.UNKNOWN

            # Check if file was recently modified (likely pending upload)
            try:
                file_path_obj = Path(normalised)
                if file_path_obj.exists():
                    mtime = file_path_obj.stat().st_mtime
                    age = time.time() - mtime
                    # If modified within last 30 seconds, assume pending
                    if age < 30:
                        return FileStatus.PENDING
            except (OSError, IOError):
                pass

            # If not in queue and within mount, it's synced
            return FileStatus.SYNCED

    def get_all_statuses(self) -> Dict[str, FileStatus]:
        """Get all file statuses in the cache."""
        with self._lock:
            return {path: qf.status for path, qf in self._cache.items()}


class NautilusSocketServer:
    """Unix socket server for Nautilus extension communication.

    Protocol (line-based text):
    Request:
        STATUS
        path\t/path/to/file
        done

    Response:
        ok
        status\tsynced|syncing|pending|error|unknown
        done
    """

    SOCKET_DIR = Path.home() / ".cache" / "proton-drive-gtk"
    SOCKET_NAME = "nautilus.sock"

    def __init__(self, status_cache: SyncStatusCache):
        self.status_cache = status_cache
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
            # Ensure directory exists
            self.SOCKET_DIR.mkdir(parents=True, exist_ok=True)

            # Remove existing socket file
            if self._socket_path.exists():
                self._socket_path.unlink()

            # Create Unix socket
            self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._socket.bind(str(self._socket_path))
            self._socket.listen(5)
            self._socket.settimeout(1.0)  # For graceful shutdown

            self._running = True
            self._thread = threading.Thread(target=self._serve, daemon=True)
            self._thread.start()

            return True

        except Exception as e:
            print(f"Failed to start Nautilus socket server: {e}")
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

        # Clean up socket file
        if self._socket_path.exists():
            try:
                self._socket_path.unlink()
            except Exception:
                pass

    def _serve(self) -> None:
        """Main server loop."""
        while self._running:
            try:
                client, _ = self._socket.accept()
                # Handle client in separate thread
                threading.Thread(
                    target=self._handle_client,
                    args=(client,),
                    daemon=True
                ).start()
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    print(f"Socket server error: {e}")
                break

    def _handle_client(self, client: socket.socket) -> None:
        """Handle a client connection."""
        try:
            client.settimeout(5.0)
            data = b""

            # Read until we get "done\n"
            while not data.endswith(b"done\n"):
                chunk = client.recv(4096)
                if not chunk:
                    break
                data += chunk

            # Parse request
            lines = data.decode("utf-8", errors="replace").strip().split("\n")
            response = self._process_request(lines)

            # Send response
            client.sendall(response.encode("utf-8"))

        except Exception as e:
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
            # Parse path from request
            file_path = None
            for line in lines[1:]:
                if line.startswith("path\t"):
                    file_path = line[5:].strip()
                    break

            if not file_path:
                return "error\nmessage\tNo path provided\ndone\n"

            status = self.status_cache.get_status(file_path)
            return f"ok\nstatus\t{status.value}\ndone\n"

        elif command == "PING":
            return "ok\npong\ndone\n"

        elif command == "LIST":
            # List all files with non-synced status
            statuses = self.status_cache.get_all_statuses()
            response = "ok\n"
            for path, status in statuses.items():
                response += f"file\t{path}\t{status.value}\n"
            response += "done\n"
            return response

        else:
            return f"error\nmessage\tUnknown command: {command}\ndone\n"


class NautilusIntegration:
    """Main integration class combining cache and server."""

    def __init__(self, mount_path: str, get_vfs_queue_func: Callable[[], Optional[dict]]):
        """Initialise Nautilus integration.

        Args:
            mount_path: Path where Proton Drive is mounted
            get_vfs_queue_func: Callable that returns rclone vfs/queue data
        """
        self.mount_path = mount_path
        self._get_vfs_queue = get_vfs_queue_func
        self.cache = SyncStatusCache(mount_path)
        self.server = NautilusSocketServer(self.cache)
        self._update_thread: Optional[threading.Thread] = None
        self._running = False

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
        queue_data = self._get_vfs_queue()
        self.cache.update_from_vfs_queue(queue_data)

    def _update_loop(self) -> None:
        """Background loop to update cache periodically."""
        while self._running:
            try:
                self.update_cache()
            except Exception as e:
                print(f"Cache update error: {e}")

            # Sleep for 2 seconds between updates
            for _ in range(20):  # 20 x 0.1s = 2s
                if not self._running:
                    break
                time.sleep(0.1)
