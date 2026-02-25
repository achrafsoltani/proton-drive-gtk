#!/usr/bin/env python3
"""Proton Drive GTK - System tray application for Proton Drive.

Supports two sync modes:
- bisync: True bidirectional sync using rclone bisync (default, recommended)
- vfs_mount: Legacy VFS mount mode (kept for compatibility)
"""

import argparse
import sys
import os

# Add src directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import get_config, SyncMode


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Proton Drive GTK - System tray application"
    )
    parser.add_argument(
        '--mode',
        choices=['bisync', 'vfs_mount'],
        default=None,
        help='Sync mode (default: from config or bisync)'
    )
    parser.add_argument(
        '--legacy',
        action='store_true',
        help='Use legacy VFS mount mode'
    )
    args = parser.parse_args()

    # Determine sync mode
    config = get_config()

    if args.legacy:
        mode = SyncMode.VFS_MOUNT
    elif args.mode:
        mode = SyncMode(args.mode)
    else:
        mode = SyncMode(config.sync_mode)

    # Run appropriate tray
    if mode == SyncMode.BISYNC:
        from bisync_tray import main as bisync_main
        bisync_main()  # Includes single-instance check
    else:
        from tray import ProtonDriveTray
        app = ProtonDriveTray()
        app.run()


if __name__ == "__main__":
    main()
