"""Configuration management for Proton Drive GTK."""

import json
import os
from pathlib import Path
from dataclasses import dataclass, asdict

CONFIG_DIR = Path.home() / ".config" / "proton-drive-gtk"
CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass
class Config:
    remote_name: str = "protondrive"
    mount_path: str = str(Path.home() / "ProtonDrive")
    auto_mount: bool = False
    vfs_cache_mode: str = "full"
    show_notifications: bool = True

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
