"""Rclone wrapper for Proton Drive operations."""

import subprocess
import shutil
import json
import os
import signal
import socket
from pathlib import Path
from enum import Enum
from typing import Optional
from dataclasses import dataclass
from config import Config

import secrets
import tempfile
import stat

RC_PORT = 5572


class MountStatus(Enum):
    UNMOUNTED = "unmounted"
    MOUNTING = "mounting"
    MOUNTED = "mounted"
    PAUSED = "paused"
    ERROR = "error"


@dataclass
class TransferStats:
    """Current transfer statistics."""
    speed: float = 0.0  # bytes per second
    transferring: int = 0  # number of active transfers
    bytes_transferred: int = 0
    errors: int = 0

    @property
    def speed_human(self) -> str:
        """Human readable speed."""
        if self.speed < 1024:
            return f"{self.speed:.0f} B/s"
        elif self.speed < 1024 * 1024:
            return f"{self.speed / 1024:.1f} KB/s"
        else:
            return f"{self.speed / (1024 * 1024):.1f} MB/s"

    @property
    def is_transferring(self) -> bool:
        return self.transferring > 0


class RcloneManager:
    """Manages rclone mount operations for Proton Drive."""

    def __init__(self, config: Config):
        self.config = config
        self._mount_process: Optional[subprocess.Popen] = None
        self._status = MountStatus.UNMOUNTED
        self._paused = False
        # Generate random credentials for RC auth (per session)
        self._rc_user = "proton"
        self._rc_pass = secrets.token_urlsafe(16)

    @property
    def status(self) -> MountStatus:
        """Get current mount status."""
        if self._paused:
            return MountStatus.PAUSED
        if self._is_mounted():
            return MountStatus.MOUNTED
        return self._status

    def _is_rc_running(self) -> bool:
        """Check if rclone rc interface is available."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(('127.0.0.1', RC_PORT))
            sock.close()
            return result == 0
        except Exception:
            return False

    def _rc_command(self, command: str, params: dict = None) -> Optional[dict]:
        """Execute an rclone rc command with authentication."""
        if not self._is_rc_running():
            return None

        try:
            cmd = [
                "rclone", "rc", command,
                f"--rc-addr=127.0.0.1:{RC_PORT}",
                f"--rc-user={self._rc_user}",
                f"--rc-pass={self._rc_pass}",
            ]
            if params:
                for key, value in params.items():
                    cmd.append(f"--json={json.dumps({key: value})}")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5
            )

            if result.returncode == 0:
                return json.loads(result.stdout) if result.stdout.strip() else {}
            return None
        except Exception:
            return None

    def _is_mounted(self) -> bool:
        """Check if the drive is currently mounted."""
        mount_path = Path(self.config.mount_path)
        if not mount_path.exists():
            return False

        # Check if it's a FUSE mount
        try:
            result = subprocess.run(
                ["mountpoint", "-q", str(mount_path)],
                capture_output=True
            )
            return result.returncode == 0
        except Exception:
            return False

    def mount(self) -> tuple[bool, str]:
        """Mount Proton Drive."""
        if self._is_mounted():
            return True, "Already mounted"

        # Ensure mount path exists
        mount_path = Path(self.config.mount_path)
        mount_path.mkdir(parents=True, exist_ok=True)

        # Build rclone command with rc enabled and authentication
        cmd = [
            "rclone", "mount",
            f"{self.config.remote_name}:",
            str(mount_path),
            f"--vfs-cache-mode={self.config.vfs_cache_mode}",
            f"--rc",
            f"--rc-addr=127.0.0.1:{RC_PORT}",
            f"--rc-user={self._rc_user}",
            f"--rc-pass={self._rc_pass}",
        ]

        self._status = MountStatus.MOUNTING
        self._paused = False

        try:
            # Start rclone in background (not using --daemon)
            self._mount_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )

            # Wait a moment for mount to establish
            import time
            time.sleep(2)

            if self._is_mounted():
                self._status = MountStatus.MOUNTED
                return True, "Mounted successfully"
            else:
                self._status = MountStatus.ERROR
                return False, "Mount failed to establish"

        except Exception as e:
            self._status = MountStatus.ERROR
            return False, str(e)

    def get_transfer_stats(self) -> TransferStats:
        """Get current transfer statistics."""
        stats = TransferStats()

        data = self._rc_command("core/stats")
        if data:
            stats.bytes_transferred = data.get("bytes", 0)
            stats.errors = data.get("errors", 0)

            # Check for active transfers in core/stats
            transferring = data.get("transferring")
            if transferring:
                stats.transferring = len(transferring)
                stats.speed = data.get("speed", 0)
            else:
                # Also check vfs/queue for pending uploads
                queue_data = self.get_vfs_queue()
                if queue_data:
                    queue = queue_data.get("queue", [])
                    uploading = [item for item in queue if item.get("uploading")]
                    if uploading:
                        stats.transferring = len(uploading)
                        stats.speed = data.get("speed", 0)
                    elif queue:
                        # Items queued but not actively uploading
                        stats.transferring = len(queue)

        return stats

    def get_vfs_queue(self) -> Optional[dict]:
        """Get the VFS upload queue status.

        Returns the queue data containing:
        - uploads: Currently uploading files
        - uploadsWaiting: Files waiting to upload
        - uploadsQueued: Files queued for upload
        """
        return self._rc_command("vfs/queue")

    def pause(self) -> tuple[bool, str]:
        """Pause all transfers."""
        if not self._is_rc_running():
            return False, "RC not available"

        result = self._rc_command("core/bwlimit", {"rate": "1K"})
        if result is not None:
            self._paused = True
            return True, "Paused"
        return False, "Failed to pause"

    def resume(self) -> tuple[bool, str]:
        """Resume all transfers."""
        if not self._is_rc_running():
            return False, "RC not available"

        result = self._rc_command("core/bwlimit", {"rate": "off"})
        if result is not None:
            self._paused = False
            return True, "Resumed"
        return False, "Failed to resume"

    @property
    def is_paused(self) -> bool:
        return self._paused

    def unmount(self, lazy: bool = True) -> tuple[bool, str]:
        """Unmount Proton Drive."""
        if not self._is_mounted():
            self._status = MountStatus.UNMOUNTED
            return True, "Not mounted"

        mount_path = self.config.mount_path

        try:
            # Try fusermount first (with lazy flag if busy)
            cmd = ["fusermount", "-u"]
            if lazy:
                cmd.append("-z")  # Lazy unmount
            cmd.append(mount_path)

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True
            )

            if result.returncode == 0:
                self._status = MountStatus.UNMOUNTED
                return True, "Unmounted successfully"

            # Fallback to umount with lazy option
            cmd = ["umount"]
            if lazy:
                cmd.append("-l")  # Lazy unmount
            cmd.append(mount_path)

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True
            )

            if result.returncode == 0:
                self._status = MountStatus.UNMOUNTED
                return True, "Unmounted successfully"

            return False, result.stderr or "Unmount failed"

        except Exception as e:
            return False, str(e)

    def get_remote_info(self) -> dict:
        """Get information about the remote."""
        try:
            result = subprocess.run(
                ["rclone", "about", f"{self.config.remote_name}:", "--json"],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0:
                import json
                return json.loads(result.stdout)
        except Exception:
            pass
        return {}

    def is_rclone_available(self) -> bool:
        """Check if rclone is installed."""
        return shutil.which("rclone") is not None

    def is_remote_configured(self) -> bool:
        """Check if the protondrive remote is configured."""
        try:
            result = subprocess.run(
                ["rclone", "listremotes"],
                capture_output=True,
                text=True
            )
            remotes = result.stdout.strip().split("\n")
            return f"{self.config.remote_name}:" in remotes
        except Exception:
            return False
