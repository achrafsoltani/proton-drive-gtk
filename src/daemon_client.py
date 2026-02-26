"""Client for communicating with the Go sync daemon via Unix socket."""

import socket
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Dict

SOCKET_PATH = Path.home() / ".cache" / "proton-drive-gtk" / "daemon.sock"


@dataclass
class DaemonStats:
    """Statistics from the sync daemon."""
    status: str
    total_files: int
    synced_files: int
    pending_upload: int
    pending_download: int
    errors: int
    current_file: Optional[str]
    is_listing: bool
    is_downloading: bool
    is_uploading: bool
    download_total: int
    download_done: int
    upload_total: int
    upload_done: int
    eta_seconds: Optional[int]


class DaemonClient:
    """Client for communicating with Go sync daemon."""

    def __init__(self, socket_path: Path = SOCKET_PATH):
        self.socket_path = socket_path

    def _send_request(self, lines: list) -> Dict[str, str]:
        """Send a request to the daemon and return parsed response."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10.0)

        try:
            sock.connect(str(self.socket_path))

            request = "\n".join(lines) + "\ndone\n"
            sock.sendall(request.encode())

            response = b""
            while not response.endswith(b"done\n"):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk

            return self._parse_response(response.decode())
        finally:
            sock.close()

    def _parse_response(self, text: str) -> Dict[str, str]:
        """Parse a tab-separated response."""
        result = {}
        for line in text.strip().split("\n"):
            if "\t" in line:
                key, value = line.split("\t", 1)
                result[key] = value
            elif line == "ok":
                result["ok"] = "true"
            elif line == "error":
                result["error"] = "true"
        return result

    def is_running(self) -> bool:
        """Check if the daemon is running."""
        try:
            resp = self._send_request(["PING"])
            return "pong" in resp or resp.get("ok") == "true"
        except (socket.error, OSError, FileNotFoundError):
            return False

    def get_stats(self) -> DaemonStats:
        """Get daemon statistics."""
        resp = self._send_request(["STATS"])
        return DaemonStats(
            status=resp.get("status", "unknown"),
            total_files=int(resp.get("total_files", 0)),
            synced_files=int(resp.get("synced_files", 0)),
            pending_upload=int(resp.get("pending_upload", 0)),
            pending_download=int(resp.get("pending_download", 0)),
            errors=int(resp.get("errors", 0)),
            current_file=resp.get("current_file"),
            is_listing=resp.get("is_listing") == "1",
            is_downloading=resp.get("is_downloading") == "1",
            is_uploading=resp.get("is_uploading") == "1",
            download_total=int(resp.get("download_total", 0)),
            download_done=int(resp.get("download_done", 0)),
            upload_total=int(resp.get("upload_total", 0)),
            upload_done=int(resp.get("upload_done", 0)),
            eta_seconds=int(resp.get("eta_seconds")) if resp.get("eta_seconds") else None,
        )

    def get_file_status(self, path: str) -> str:
        """Get the sync status of a file."""
        resp = self._send_request(["STATUS", f"path\t{path}"])
        return resp.get("status", "unknown")

    def force_sync(self) -> bool:
        """Force an immediate sync."""
        try:
            resp = self._send_request(["SYNC"])
            return resp.get("ok") == "true"
        except (socket.error, OSError):
            return False

    def pause(self) -> bool:
        """Pause syncing."""
        try:
            resp = self._send_request(["PAUSE"])
            return resp.get("ok") == "true"
        except (socket.error, OSError):
            return False

    def resume(self) -> bool:
        """Resume syncing."""
        try:
            resp = self._send_request(["RESUME"])
            return resp.get("ok") == "true"
        except (socket.error, OSError):
            return False

    def clear_cache(self) -> bool:
        """Clear the remote files cache."""
        try:
            resp = self._send_request(["CLEAR_CACHE"])
            return resp.get("ok") == "true"
        except (socket.error, OSError):
            return False
