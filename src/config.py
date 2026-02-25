"""Configuration management for Proton Drive GTK."""

import json
import os
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import Dict
from enum import Enum


CONFIG_DIR = Path.home() / ".config" / "proton-drive-gtk"
CONFIG_FILE = CONFIG_DIR / "config.json"
CACHE_DIR = Path.home() / ".cache" / "proton-drive-gtk"


class SyncMode(Enum):
    """Synchronisation mode."""
    VFS_MOUNT = "vfs_mount"  # Legacy: rclone VFS mount
    BISYNC = "bisync"        # New: rclone bisync (true sync)


@dataclass
class Config:
    remote_name: str = "protondrive"
    mount_path: str = str(Path.home() / "ProtonDrive")  # Also used as sync folder
    auto_mount: bool = False
    vfs_cache_mode: str = "full"
    show_notifications: bool = True

    # Bisync mode settings
    sync_mode: str = "bisync"  # "vfs_mount" or "bisync"
    sync_interval: int = 60  # Seconds between remote checks
    conflict_resolution: str = "newer"  # "newer", "local", "remote"
    start_on_login: bool = False

    # Selective sync (folder -> enabled)
    selective_sync_enabled: bool = False
    selective_sync_folders: Dict[str, bool] = field(default_factory=dict)

    @property
    def is_bisync_mode(self) -> bool:
        """Check if using bisync mode."""
        return self.sync_mode == SyncMode.BISYNC.value

    @property
    def db_path(self) -> Path:
        """Path to the sync state database."""
        return CACHE_DIR / "sync_state.db"

    @classmethod
    def load(cls) -> "Config":
        """Load config from file or return defaults."""
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE) as f:
                    data = json.load(f)
                return cls(**data)
            except (json.JSONDecodeError, TypeError):
                pass
        return cls()

    def save(self) -> None:
        """Save config to file."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(asdict(self), f, indent=2)


def get_config() -> Config:
    """Get the current configuration."""
    return Config.load()
