#!/usr/bin/env python3
"""Proton Drive GTK - System tray application for Proton Drive."""

import sys
import os

# Add src directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tray import ProtonDriveTray


def main():
    """Main entry point."""
    app = ProtonDriveTray()
    app.run()


if __name__ == "__main__":
    main()
