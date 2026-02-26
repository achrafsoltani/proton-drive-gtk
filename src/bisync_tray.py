"""System tray implementation for bisync mode using AppIndicator."""

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('AyatanaAppIndicator3', '0.1')

from gi.repository import Gtk, GLib, AyatanaAppIndicator3 as AppIndicator
import signal
import subprocess
import os
import sys
import fcntl
from pathlib import Path
from typing import Optional

# Lock file for single instance
LOCK_FILE = Path.home() / ".cache" / "proton-drive-gtk" / "tray.lock"


class SingleInstance:
    """Ensure only one instance of the application runs."""

    def __init__(self):
        self.lock_file = None
        self.lock_fd = None

    def acquire(self) -> bool:
        """Try to acquire the lock. Returns True if successful."""
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)

        try:
            self.lock_fd = open(LOCK_FILE, 'w')
            fcntl.flock(self.lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Write PID to lock file
            self.lock_fd.write(str(os.getpid()))
            self.lock_fd.flush()
            return True
        except (IOError, OSError):
            # Another instance is running
            if self.lock_fd:
                self.lock_fd.close()
            return False

    def release(self):
        """Release the lock."""
        if self.lock_fd:
            try:
                fcntl.flock(self.lock_fd.fileno(), fcntl.LOCK_UN)
                self.lock_fd.close()
                LOCK_FILE.unlink(missing_ok=True)
            except (IOError, OSError):
                pass

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from daemon import SyncDaemon, StateDatabase, SyncStatus
from daemon.sync_daemon import DaemonStatus
from config import Config, get_config
from nautilus_server import NautilusSocketServer, SyncStatusCache
from daemon_client import DaemonClient, DaemonStats

# Path to Go daemon binary (development and installed locations)
GO_DAEMON_BINARY = Path(__file__).parent.parent / "go-daemon" / "bin" / "proton-sync-daemon"
GO_DAEMON_INSTALLED = Path("/usr/share/proton-drive-gtk/bin/proton-sync-daemon")
GO_DAEMON_LOCAL = Path(__file__).parent / "bin" / "proton-sync-daemon"  # For when run from /usr/share


class BisyncSettingsDialog(Gtk.Dialog):
    """Settings dialog for Proton Drive sync."""

    def __init__(self, config: Config):
        super().__init__(
            title="Proton Drive Preferences",
            flags=Gtk.DialogFlags.MODAL
        )
        self.config = config
        self.set_default_size(400, 300)

        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Save", Gtk.ResponseType.OK)

        content = self.get_content_area()
        content.set_spacing(12)
        content.set_margin_start(20)
        content.set_margin_end(20)
        content.set_margin_top(20)
        content.set_margin_bottom(20)

        # Sync folder
        folder_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        folder_label = Gtk.Label(label="Sync Folder:")
        folder_label.set_xalign(0)
        folder_label.set_size_request(120, -1)
        self.folder_entry = Gtk.Entry()
        self.folder_entry.set_text(str(config.mount_path))
        self.folder_entry.set_hexpand(True)
        folder_box.pack_start(folder_label, False, False, 0)
        folder_box.pack_start(self.folder_entry, True, True, 0)
        content.pack_start(folder_box, False, False, 0)

        # Remote name
        remote_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        remote_label = Gtk.Label(label="Remote Name:")
        remote_label.set_xalign(0)
        remote_label.set_size_request(120, -1)
        self.remote_entry = Gtk.Entry()
        self.remote_entry.set_text(config.remote_name)
        self.remote_entry.set_hexpand(True)
        remote_box.pack_start(remote_label, False, False, 0)
        remote_box.pack_start(self.remote_entry, True, True, 0)
        content.pack_start(remote_box, False, False, 0)

        # Sync interval
        interval_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        interval_label = Gtk.Label(label="Sync Interval:")
        interval_label.set_xalign(0)
        interval_label.set_size_request(120, -1)
        self.interval_spin = Gtk.SpinButton.new_with_range(10, 3600, 10)
        self.interval_spin.set_value(config.sync_interval)
        interval_unit = Gtk.Label(label="seconds")
        interval_box.pack_start(interval_label, False, False, 0)
        interval_box.pack_start(self.interval_spin, False, False, 0)
        interval_box.pack_start(interval_unit, False, False, 0)
        content.pack_start(interval_box, False, False, 0)

        # Info label
        info_label = Gtk.Label()
        info_label.set_markup(
            "<small>Changes will take effect after restarting the sync daemon.</small>"
        )
        info_label.set_xalign(0)
        content.pack_start(info_label, False, False, 10)

        self.show_all()

    def save_config(self):
        """Save the configuration."""
        self.config.mount_path = self.folder_entry.get_text()
        self.config.remote_name = self.remote_entry.get_text()
        self.config.sync_interval = int(self.interval_spin.get_value())
        self.config.save()


class BisyncTray:
    """System tray application for Proton Drive bisync mode."""

    ICON_IDLE = "network-offline-symbolic"
    ICON_SYNCING = "network-transmit-receive-symbolic"
    ICON_SYNCED = "network-idle-symbolic"
    ICON_ERROR = "network-error-symbolic"
    ICON_PAUSED = "media-playback-pause-symbolic"

    def __init__(self):
        self.config = get_config()
        self.daemon: Optional[SyncDaemon] = None
        self.daemon_process: Optional[subprocess.Popen] = None
        self.daemon_client: Optional[DaemonClient] = None
        self.nautilus_server: Optional[NautilusSocketServer] = None
        self._use_go_daemon = self.config.use_go_daemon

        # Create indicator
        self.indicator = AppIndicator.Indicator.new(
            "proton-drive-gtk",
            self.ICON_IDLE,
            AppIndicator.IndicatorCategory.APPLICATION_STATUS
        )
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.indicator.set_title("Proton Drive")

        # Build menu
        self.menu = self._build_menu()
        self.indicator.set_menu(self.menu)

        # Cache for recent files (avoid rebuilding every update)
        self._recent_files_cache = []
        self._recent_files_updated = 0

        # Update status periodically (lightweight)
        GLib.timeout_add_seconds(5, self._update_status_light)

    def _build_menu(self) -> Gtk.Menu:
        """Build the context menu (Dropbox-style)."""
        menu = Gtk.Menu()

        # Connect to show signal for lazy loading
        menu.connect("show", self._on_menu_show)

        # === Primary Actions ===
        # Open folder
        open_folder_item = Gtk.MenuItem(label="Open Proton Drive Folder")
        open_folder_item.connect("activate", self._on_open_folder)
        menu.append(open_folder_item)

        # Open website
        open_web_item = Gtk.MenuItem(label="Launch Proton Drive Website")
        open_web_item.connect("activate", self._on_open_website)
        menu.append(open_web_item)

        menu.append(Gtk.SeparatorMenuItem())

        # === Account Section ===
        account_label = Gtk.MenuItem(label="Account")
        account_label.set_sensitive(False)
        menu.append(account_label)

        # Storage info
        self.storage_item = Gtk.MenuItem(label="    Calculating...")
        self.storage_item.set_sensitive(False)
        menu.append(self.storage_item)

        # Recently changed files submenu
        recent_item = Gtk.MenuItem(label="    Recently Changed Files")
        recent_submenu = Gtk.Menu()
        self.recent_files_menu = recent_submenu
        # Connect to show signal to populate lazily
        recent_submenu.connect("show", lambda w: self._update_recent_files())
        # Placeholder - will be populated dynamically
        no_recent = Gtk.MenuItem(label="No recent changes")
        no_recent.set_sensitive(False)
        recent_submenu.append(no_recent)
        recent_submenu.show_all()
        recent_item.set_submenu(recent_submenu)
        menu.append(recent_item)

        menu.append(Gtk.SeparatorMenuItem())

        # === Status Section ===
        # Status (greyed out like Dropbox's "Up to date")
        self.status_item = Gtk.MenuItem(label="Up to date")
        self.status_item.set_sensitive(False)
        menu.append(self.status_item)

        # Pause/Resume toggle
        self.pause_item = Gtk.MenuItem(label="Pause Syncing")
        self.pause_item.connect("activate", self._on_pause_toggle)
        menu.append(self.pause_item)

        # Sync Now (hidden by default, shown when paused or idle)
        self.sync_now_item = Gtk.MenuItem(label="Sync Now")
        self.sync_now_item.connect("activate", self._on_sync_now)
        menu.append(self.sync_now_item)

        # Check for remote changes
        check_remote_item = Gtk.MenuItem(label="Check for Remote Changes")
        check_remote_item.connect("activate", self._on_check_remote)
        menu.append(check_remote_item)

        menu.append(Gtk.SeparatorMenuItem())

        # === Settings Section ===
        # Preferences
        settings_item = Gtk.MenuItem(label="Preferences...")
        settings_item.connect("activate", self._on_settings)
        menu.append(settings_item)

        # View errors (shown only when errors exist)
        self.errors_item = Gtk.MenuItem(label="View Errors...")
        self.errors_item.connect("activate", self._on_view_errors)
        self.errors_item.set_visible(False)
        menu.append(self.errors_item)

        # View conflicts (shown only when conflicts exist)
        self.conflicts_item = Gtk.MenuItem(label="View Conflicts...")
        self.conflicts_item.connect("activate", self._on_view_conflicts)
        self.conflicts_item.set_visible(False)
        menu.append(self.conflicts_item)

        # Export logs
        logs_item = Gtk.MenuItem(label="Export Debug Logs")
        logs_item.connect("activate", self._on_export_logs)
        menu.append(logs_item)

        menu.append(Gtk.SeparatorMenuItem())

        # Quit
        quit_item = Gtk.MenuItem(label="Quit Proton Drive")
        quit_item.connect("activate", self._on_quit)
        menu.append(quit_item)

        menu.show_all()
        return menu

    def _format_eta(self, seconds: int) -> str:
        """Format ETA seconds into human-readable string."""
        if seconds < 60:
            return f"{seconds}s"
        elif seconds < 3600:
            mins = seconds // 60
            return f"{mins}m"
        else:
            hours = seconds // 3600
            mins = (seconds % 3600) // 60
            return f"{hours}h {mins}m"

    def _update_status(self) -> bool:
        """Update the tray status and icon (Dropbox-style)."""
        if self._use_go_daemon:
            return self._update_status_go()
        else:
            return self._update_status_python()

    def _update_status_go(self) -> bool:
        """Update status from Go daemon."""
        if not self.daemon_client:
            self.indicator.set_icon_full(self.ICON_IDLE, "Not running")
            self.status_item.set_label("Not running")
            self.storage_item.set_label("    --")
            return True

        try:
            stats = self.daemon_client.get_stats()
        except Exception:
            self.indicator.set_icon_full(self.ICON_ERROR, "Connection lost")
            self.status_item.set_label("Daemon not responding")
            return True

        # Update storage/files info
        if stats.total_files > 0:
            self.storage_item.set_label(f"    {stats.synced_files:,} of {stats.total_files:,} files synced")
        else:
            self.storage_item.set_label("    Calculating...")

        # Activity flags take priority over status string — they reflect
        # real-time daemon state and avoid "Up to date" during active work.
        status = stats.status.lower()

        if stats.is_listing:
            self.indicator.set_icon_full(self.ICON_SYNCING, "Indexing")
            self.pause_item.set_label("Pause Syncing")
            self.status_item.set_label("Indexing files...")
        elif stats.is_downloading:
            self.indicator.set_icon_full(self.ICON_SYNCING, "Downloading")
            self.pause_item.set_label("Pause Syncing")
            pct = int(100 * stats.download_done / stats.download_total) if stats.download_total > 0 else 0
            eta_str = f" ({self._format_eta(stats.eta_seconds)})" if stats.eta_seconds else ""
            self.status_item.set_label(f"Downloading {pct}%{eta_str}")
        elif stats.is_uploading:
            self.indicator.set_icon_full(self.ICON_SYNCING, "Uploading")
            self.pause_item.set_label("Pause Syncing")
            pct = int(100 * stats.upload_done / stats.upload_total) if stats.upload_total > 0 else 0
            eta_str = f" ({self._format_eta(stats.eta_seconds)})" if stats.eta_seconds else ""
            self.status_item.set_label(f"Uploading {pct}%{eta_str}")
        elif status == "syncing":
            self.indicator.set_icon_full(self.ICON_SYNCING, "Syncing")
            self.pause_item.set_label("Pause Syncing")
            if stats.current_file:
                filename = os.path.basename(stats.current_file)
                self.status_item.set_label(f"Syncing {filename[:30]}...")
            else:
                self.status_item.set_label("Syncing...")
        elif status == "paused":
            self.indicator.set_icon_full(self.ICON_PAUSED, "Paused")
            self.status_item.set_label("Syncing paused")
            self.pause_item.set_label("Resume Syncing")
        elif status == "error":
            self.indicator.set_icon_full(self.ICON_ERROR, "Error")
            self.status_item.set_label("Sync error")
            self.pause_item.set_label("Pause Syncing")
        elif status == "running":
            self.pause_item.set_label("Pause Syncing")
            if stats.pending_upload > 0 or stats.pending_download > 0:
                self.indicator.set_icon_full(self.ICON_SYNCING, "Pending")
                self.status_item.set_label(f"Syncing {stats.pending_upload + stats.pending_download} items...")
            else:
                self.indicator.set_icon_full(self.ICON_SYNCED, "Up to date")
                self.status_item.set_label("Up to date")
        else:
            self.indicator.set_icon_full(self.ICON_IDLE, "Starting")
            self.status_item.set_label("Starting...")
            self.pause_item.set_label("Pause Syncing")

        # Show/hide errors
        self.errors_item.set_visible(stats.errors > 0)
        if stats.errors > 0:
            self.errors_item.set_label(f"View Errors ({stats.errors})...")

        return True

    def _update_status_python(self) -> bool:
        """Update status from Python daemon."""
        if self.daemon is None:
            self.indicator.set_icon_full(self.ICON_IDLE, "Not running")
            self.status_item.set_label("Not running")
            self.storage_item.set_label("    --")
            return True

        stats = self.daemon.get_stats()

        # Update storage/files info
        if stats.total_files > 0:
            self.storage_item.set_label(f"    {stats.synced_files:,} of {stats.total_files:,} files synced")
        else:
            self.storage_item.set_label("    Calculating...")

        # Activity flags take priority over status string
        if stats.is_listing:
            self.indicator.set_icon_full(self.ICON_SYNCING, "Indexing")
            self.pause_item.set_label("Pause Syncing")
            self.status_item.set_label(
                f"Indexing {stats.listing_files:,} files..."
            )
        elif stats.is_downloading:
            self.indicator.set_icon_full(self.ICON_SYNCING, "Downloading")
            self.pause_item.set_label("Pause Syncing")
            pct = int(100 * stats.download_done / stats.download_total) if stats.download_total > 0 else 0
            eta_str = f" ({self._format_eta(stats.eta_seconds)})" if stats.eta_seconds else ""
            self.status_item.set_label(f"Downloading {pct}%{eta_str}")
        elif stats.is_uploading:
            self.indicator.set_icon_full(self.ICON_SYNCING, "Uploading")
            self.pause_item.set_label("Pause Syncing")
            pct = int(100 * stats.upload_done / stats.upload_total) if stats.upload_total > 0 else 0
            eta_str = f" ({self._format_eta(stats.eta_seconds)})" if stats.eta_seconds else ""
            self.status_item.set_label(f"Uploading {pct}%{eta_str}")
        elif stats.status == DaemonStatus.SYNCING:
            self.indicator.set_icon_full(self.ICON_SYNCING, "Syncing")
            self.pause_item.set_label("Pause Syncing")
            if stats.current_file:
                filename = os.path.basename(stats.current_file)
                self.status_item.set_label(f"Syncing {filename[:30]}...")
            else:
                self.status_item.set_label("Syncing...")
        elif stats.status == DaemonStatus.PAUSED:
            self.indicator.set_icon_full(self.ICON_PAUSED, "Paused")
            self.status_item.set_label("Syncing paused")
            self.pause_item.set_label("Resume Syncing")
        elif stats.status == DaemonStatus.ERROR:
            self.indicator.set_icon_full(self.ICON_ERROR, "Error")
            self.status_item.set_label("Sync error")
            self.pause_item.set_label("Pause Syncing")
        elif stats.status == DaemonStatus.RUNNING:
            self.pause_item.set_label("Pause Syncing")
            if stats.pending_upload > 0 or stats.pending_download > 0:
                self.indicator.set_icon_full(self.ICON_SYNCING, "Pending")
                self.status_item.set_label(f"Syncing {stats.pending_upload + stats.pending_download} items...")
            else:
                self.indicator.set_icon_full(self.ICON_SYNCED, "Up to date")
                self.status_item.set_label("Up to date")
        else:
            self.indicator.set_icon_full(self.ICON_IDLE, "Starting")
            self.status_item.set_label("Starting...")
            self.pause_item.set_label("Pause Syncing")

        # Show/hide conflicts and errors
        self.conflicts_item.set_visible(stats.conflicts > 0)
        if stats.conflicts > 0:
            self.conflicts_item.set_label(f"View Conflicts ({stats.conflicts})...")

        self.errors_item.set_visible(stats.errors > 0)
        if stats.errors > 0:
            self.errors_item.set_label(f"View Errors ({stats.errors})...")

        return True  # Continue the timeout

    def _update_status_light(self) -> bool:
        """Lightweight status update - only updates icon and status label."""
        if self._use_go_daemon:
            return self._update_status_light_go()
        else:
            return self._update_status_light_python()

    def _update_status_light_go(self) -> bool:
        """Lightweight status update for Go daemon - updates icon AND menu labels."""
        if not self.daemon_client:
            self.indicator.set_icon_full(self.ICON_IDLE, "Not running")
            self.status_item.set_label("Not running")
            return True

        try:
            stats = self.daemon_client.get_stats()
        except Exception:
            self.indicator.set_icon_full(self.ICON_ERROR, "Connection lost")
            self.status_item.set_label("Daemon not responding")
            return True

        # Update storage info
        if stats.total_files > 0:
            self.storage_item.set_label(f"    {stats.synced_files:,} of {stats.total_files:,} files synced")

        # Activity flags take priority — same logic as full update
        status = stats.status.lower()

        if stats.is_listing:
            self.indicator.set_icon_full(self.ICON_SYNCING, "Indexing")
            self.status_item.set_label("Indexing files...")
        elif stats.is_downloading:
            self.indicator.set_icon_full(self.ICON_SYNCING, "Downloading")
            pct = int(100 * stats.download_done / stats.download_total) if stats.download_total > 0 else 0
            self.status_item.set_label(f"Downloading {pct}%")
        elif stats.is_uploading:
            self.indicator.set_icon_full(self.ICON_SYNCING, "Uploading")
            pct = int(100 * stats.upload_done / stats.upload_total) if stats.upload_total > 0 else 0
            self.status_item.set_label(f"Uploading {pct}%")
        elif status == "syncing":
            self.indicator.set_icon_full(self.ICON_SYNCING, "Syncing")
            self.status_item.set_label("Syncing...")
        elif status == "paused":
            self.indicator.set_icon_full(self.ICON_PAUSED, "Paused")
            self.status_item.set_label("Syncing paused")
        elif status == "error":
            self.indicator.set_icon_full(self.ICON_ERROR, "Error")
            self.status_item.set_label("Sync error")
        elif status == "running":
            if stats.pending_upload > 0 or stats.pending_download > 0:
                self.indicator.set_icon_full(self.ICON_SYNCING, "Pending")
                self.status_item.set_label(f"Syncing {stats.pending_upload + stats.pending_download} items...")
            else:
                self.indicator.set_icon_full(self.ICON_SYNCED, "Up to date")
                self.status_item.set_label("Up to date")
        else:
            self.indicator.set_icon_full(self.ICON_IDLE, "Starting")
            self.status_item.set_label("Starting...")

        # Update errors visibility
        self.errors_item.set_visible(stats.errors > 0)
        if stats.errors > 0:
            self.errors_item.set_label(f"View Errors ({stats.errors})...")

        return True

    def _update_status_light_python(self) -> bool:
        """Lightweight status update for Python daemon."""
        if self.daemon is None:
            self.indicator.set_icon_full(self.ICON_IDLE, "Not running")
            return True

        stats = self.daemon.get_stats()

        # Activity flags take priority
        if stats.is_listing:
            self.indicator.set_icon_full(self.ICON_SYNCING, "Indexing")
            self.status_item.set_label(f"Indexing {stats.listing_files:,} files...")
        elif stats.is_downloading:
            self.indicator.set_icon_full(self.ICON_SYNCING, "Downloading")
            pct = int(100 * stats.download_done / stats.download_total) if stats.download_total > 0 else 0
            self.status_item.set_label(f"Downloading {pct}%")
        elif stats.is_uploading:
            self.indicator.set_icon_full(self.ICON_SYNCING, "Uploading")
            pct = int(100 * stats.upload_done / stats.upload_total) if stats.upload_total > 0 else 0
            self.status_item.set_label(f"Uploading {pct}%")
        elif stats.status == DaemonStatus.SYNCING:
            self.indicator.set_icon_full(self.ICON_SYNCING, "Syncing")
            self.status_item.set_label("Syncing...")
        elif stats.status == DaemonStatus.PAUSED:
            self.indicator.set_icon_full(self.ICON_PAUSED, "Paused")
            self.status_item.set_label("Syncing paused")
        elif stats.status == DaemonStatus.ERROR:
            self.indicator.set_icon_full(self.ICON_ERROR, "Error")
            self.status_item.set_label("Sync error")
        elif stats.status == DaemonStatus.RUNNING:
            if stats.pending_upload > 0 or stats.pending_download > 0:
                self.indicator.set_icon_full(self.ICON_SYNCING, "Pending")
                self.status_item.set_label(f"Syncing {stats.pending_upload + stats.pending_download} items...")
            else:
                self.indicator.set_icon_full(self.ICON_SYNCED, "Up to date")
                self.status_item.set_label("Up to date")
        else:
            self.indicator.set_icon_full(self.ICON_IDLE, "Starting")
            self.status_item.set_label("Starting...")

        return True  # Continue the timeout

    def _on_menu_show(self, menu):
        """Called when menu is about to be shown - do full status update."""
        # Run full status update (but not recent files - that's on submenu show)
        # Use idle_add to ensure it runs on GTK main thread
        GLib.idle_add(self._update_status)

    def _update_recent_files(self):
        """Update the recently changed files submenu (called on submenu show)."""
        import time
        # Only update if more than 10 seconds since last update
        now = time.time()
        if now - self._recent_files_updated < 10:
            return
        self._recent_files_updated = now

        # Clear existing items
        for child in self.recent_files_menu.get_children():
            self.recent_files_menu.remove(child)

        # Get recent sync history (only available with Python daemon)
        if self._use_go_daemon:
            # Go daemon doesn't expose recent files yet
            no_recent = Gtk.MenuItem(label="No recent changes")
            no_recent.set_sensitive(False)
            self.recent_files_menu.append(no_recent)
        elif self.daemon:
            try:
                recent = self.daemon.db.get_recent_history(limit=10)
                if recent:
                    for entry in recent:
                        filename = os.path.basename(entry.path)
                        # Truncate long names
                        if len(filename) > 40:
                            filename = filename[:37] + "..."
                        item = Gtk.MenuItem(label=filename)
                        item.connect("activate", self._on_open_recent_file, entry.path)
                        self.recent_files_menu.append(item)
                else:
                    no_recent = Gtk.MenuItem(label="No recent changes")
                    no_recent.set_sensitive(False)
                    self.recent_files_menu.append(no_recent)
            except Exception:
                no_recent = Gtk.MenuItem(label="No recent changes")
                no_recent.set_sensitive(False)
                self.recent_files_menu.append(no_recent)
        else:
            no_recent = Gtk.MenuItem(label="No recent changes")
            no_recent.set_sensitive(False)
            self.recent_files_menu.append(no_recent)

        self.recent_files_menu.show_all()

    def _on_open_recent_file(self, widget, filepath):
        """Open a recent file in the file manager."""
        path = Path(filepath)
        if path.exists():
            # Open the parent folder with the file selected
            subprocess.Popen(["xdg-open", str(path.parent)])
        else:
            self._show_error("File Not Found", f"The file no longer exists:\n{filepath}")

    def _on_sync_now(self, widget):
        """Handle sync now action."""
        if self._use_go_daemon:
            if self.daemon_client:
                if not self.daemon_client.force_sync():
                    self._show_error("Sync Failed", "Failed to trigger sync")
        elif self.daemon:
            success, message = self.daemon.force_sync()
            if not success:
                self._show_error("Sync Failed", message)

    def _on_pause_toggle(self, widget):
        """Handle pause/resume toggle."""
        if self._use_go_daemon:
            if self.daemon_client:
                try:
                    stats = self.daemon_client.get_stats()
                    if stats.status.lower() == "paused":
                        if not self.daemon_client.resume():
                            self._show_error("Resume Failed", "Failed to resume sync")
                    else:
                        if not self.daemon_client.pause():
                            self._show_error("Pause Failed", "Failed to pause sync")
                except Exception as e:
                    self._show_error("Error", str(e))
        elif self.daemon:
            if self.daemon.status == DaemonStatus.PAUSED:
                success, message = self.daemon.resume()
                if not success:
                    self._show_error("Resume Failed", message)
            else:
                success, message = self.daemon.pause()
                if not success:
                    self._show_error("Pause Failed", message)

        self._update_status()

    def _on_check_remote(self, widget):
        """Check for remote changes immediately."""
        if self._use_go_daemon:
            if self.daemon_client:
                # Clear cache then trigger sync
                self.daemon_client.clear_cache()
                if self.daemon_client.force_sync():
                    self.status_item.set_label("Checking remote...")
                else:
                    self._show_error("Check Failed", "Failed to trigger remote check")
        elif self.daemon:
            # Clear the cache to force a fresh remote listing
            self.daemon.db.clear_remote_files_cache()
            success, message = self.daemon.force_sync()
            if success:
                self.status_item.set_label("Checking remote...")
            else:
                self._show_error("Check Failed", message)

    def _on_open_folder(self, widget):
        """Open the sync folder in file manager."""
        sync_path = self.config.mount_path
        if Path(sync_path).exists():
            subprocess.Popen(["xdg-open", sync_path])
        else:
            self._show_error(
                "Folder Not Found",
                f"The sync folder does not exist:\n{sync_path}"
            )

    def _on_open_website(self, widget):
        """Open Proton Drive website in browser."""
        subprocess.Popen(["xdg-open", "https://drive.proton.me"])

    def _on_export_logs(self, widget):
        """Export debug logs to a file."""
        import shutil
        from datetime import datetime

        log_source = Path("/tmp/proton-drive-gtk.log")
        if not log_source.exists():
            self._show_error("No Logs", "No log file found.")
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_dest = Path.home() / f"proton-drive-debug-{timestamp}.log"

        try:
            shutil.copy(log_source, log_dest)
            subprocess.Popen(["xdg-open", str(log_dest.parent)])
            dialog = Gtk.MessageDialog(
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                text="Logs Exported"
            )
            dialog.format_secondary_text(f"Debug logs saved to:\n{log_dest}")
            dialog.run()
            dialog.destroy()
        except Exception as e:
            self._show_error("Export Failed", str(e))

    def _on_view_conflicts(self, widget):
        """Show conflicts dialog."""
        dialog = Gtk.MessageDialog(
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK,
            text="Sync Conflicts"
        )

        if self._use_go_daemon:
            # Go daemon doesn't expose conflict list yet
            dialog.format_secondary_text("No conflicts found.")
        elif self.daemon:
            conflicts = self.daemon.db.get_files_by_status(SyncStatus.CONFLICT)
            if conflicts:
                text = "The following files have conflicts:\n\n"
                text += "\n".join(f"• {os.path.basename(p)}" for p in conflicts[:10])
                if len(conflicts) > 10:
                    text += f"\n\n...and {len(conflicts) - 10} more"
                dialog.format_secondary_text(text)
            else:
                dialog.format_secondary_text("No conflicts found.")
        else:
            dialog.format_secondary_text("Daemon not running.")

        dialog.run()
        dialog.destroy()

    def _on_view_errors(self, widget):
        """Show errors dialog."""
        dialog = Gtk.MessageDialog(
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text="Sync Errors"
        )

        if self._use_go_daemon:
            # Go daemon shows error count but not details yet
            if self.daemon_client:
                try:
                    stats = self.daemon_client.get_stats()
                    if stats.errors > 0:
                        dialog.format_secondary_text(
                            f"There are {stats.errors} file(s) with errors.\n\n"
                            "Check the log file for details:\n"
                            "/tmp/proton-drive-gtk.log"
                        )
                    else:
                        dialog.format_secondary_text("No errors found.")
                except Exception:
                    dialog.format_secondary_text("Could not retrieve error information.")
            else:
                dialog.format_secondary_text("Daemon not running.")
        elif self.daemon:
            errors = self.daemon.db.get_files_by_status(SyncStatus.ERROR)
            if errors:
                text = "The following files have errors:\n\n"
                for path in errors[:10]:
                    state = self.daemon.db.get_file_state(path)
                    name = os.path.basename(path)
                    error = state.error_message if state else "Unknown error"
                    text += f"• {name}: {error}\n"
                if len(errors) > 10:
                    text += f"\n...and {len(errors) - 10} more"
                dialog.format_secondary_text(text)
            else:
                dialog.format_secondary_text("No errors found.")
        else:
            dialog.format_secondary_text("Daemon not running.")

        dialog.run()
        dialog.destroy()

    def _on_settings(self, widget):
        """Open settings dialog."""
        GLib.idle_add(self._show_settings_dialog)

    def _show_settings_dialog(self):
        """Show settings dialog (called from idle)."""
        dialog = BisyncSettingsDialog(self.config)
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            dialog.save_config()
            self.config = get_config()
            # Restart daemon with new config
            if self._use_go_daemon:
                if self.daemon_process:
                    self.daemon_process.terminate()
                    try:
                        self.daemon_process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.daemon_process.kill()
                self._start_daemon()
            elif self.daemon:
                self.daemon.stop()
                self._start_daemon()
        dialog.destroy()
        return False

    def _on_force_resync(self, widget):
        """Force a full resync after confirmation."""
        dialog = Gtk.MessageDialog(
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.YES_NO,
            text="Force Full Resync?"
        )
        dialog.format_secondary_text(
            "This will re-scan all files and rebuild the sync state.\n\n"
            "Use this if sync state is corrupted or files are out of sync.\n\n"
            "This may take a while for large folders."
        )

        response = dialog.run()
        dialog.destroy()

        if response == Gtk.ResponseType.YES and self.daemon:
            success, message = self.daemon.force_resync()
            if not success:
                self._show_error("Resync Failed", message)

    def _on_quit(self, widget):
        """Quit the application."""
        if self.nautilus_server:
            self.nautilus_server.stop()
        if self.daemon:
            self.daemon.stop()
        if self.daemon_process:
            self.daemon_process.terminate()
            try:
                self.daemon_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.daemon_process.kill()
        Gtk.main_quit()

    def _show_error(self, title: str, message: str):
        """Show an error dialog."""
        dialog = Gtk.MessageDialog(
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text=title
        )
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()

    def _start_daemon(self) -> bool:
        """Start the sync daemon."""
        if self._use_go_daemon:
            return self._start_go_daemon()
        else:
            return self._start_python_daemon()

    def _start_go_daemon(self) -> bool:
        """Start the Go sync daemon."""
        # First check if a daemon is already running
        self.daemon_client = DaemonClient()
        if self.daemon_client.is_running():
            print("Connected to existing Go daemon")
            self._start_nautilus_server()
            return True

        # Find the Go daemon binary (check multiple locations)
        daemon_bin = None
        for path in [GO_DAEMON_BINARY, GO_DAEMON_INSTALLED, GO_DAEMON_LOCAL]:
            if path.exists():
                daemon_bin = path
                break

        if not daemon_bin:
            print("Go daemon not found, falling back to Python daemon")
            self._use_go_daemon = False
            return self._start_python_daemon()

        # Start the Go daemon process
        try:
            cmd = [
                str(daemon_bin),
                f"--max-transfers={self.config.max_concurrent_transfers}",
            ]
            self.daemon_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print(f"Started Go daemon (PID: {self.daemon_process.pid}, max-transfers={self.config.max_concurrent_transfers})")

            # Wait a moment for daemon to start
            import time
            time.sleep(2)  # Wait longer for initialization

            # Check if daemon is running
            if not self.daemon_client.is_running():
                print("Go daemon failed to start, falling back to Python daemon")
                self._use_go_daemon = False
                return self._start_python_daemon()

            print("Go daemon started successfully")

        except Exception as e:
            print(f"Failed to start Go daemon: {e}, falling back to Python daemon")
            self._use_go_daemon = False
            return self._start_python_daemon()

        # Start Nautilus integration (still uses Python server for now)
        self._start_nautilus_server()
        return True

    def _start_python_daemon(self) -> bool:
        """Start the Python sync daemon."""
        self.daemon = SyncDaemon(
            local_path=self.config.mount_path,
            remote_name=self.config.remote_name,
            db_path=self.config.db_path,
            on_status_change=self._on_daemon_status_change
        )

        # Update sync interval from config
        self.daemon.REMOTE_CHECK_INTERVAL = float(self.config.sync_interval)

        success, message = self.daemon.start()
        if not success:
            self._show_error("Start Failed", message)
            return False

        # Start Nautilus integration
        self._start_nautilus_server()
        return True

    def _start_nautilus_server(self):
        """Start the Nautilus integration server."""
        try:
            status_cache = SyncStatusCache(
                mount_path=str(self.config.mount_path),
                remote_name=self.config.remote_name
            )
            self.nautilus_server = NautilusSocketServer(
                status_cache=status_cache,
                mount_path=str(self.config.mount_path),
                remote_name=self.config.remote_name
            )
            if self.nautilus_server.start():
                print(f"Nautilus integration started at {self.nautilus_server.socket_path}")
            else:
                print("Warning: Failed to start Nautilus integration")
        except Exception as e:
            print(f"Warning: Nautilus integration error: {e}")

    def _on_daemon_status_change(self, status: DaemonStatus):
        """Handle daemon status changes."""
        # Update UI on main thread
        GLib.idle_add(self._update_status)

    def run(self):
        """Start the application."""
        # Check prerequisites
        if not self._check_rclone():
            return

        # Start daemon
        if not self._start_daemon():
            return

        # Setup signal handlers for pause/resume from terminal
        def handle_pause(signum, frame):
            if self._use_go_daemon and self.daemon_client:
                GLib.idle_add(lambda: self.daemon_client.pause())
                print("Sync paused (send SIGUSR2 to resume)")
            elif self.daemon:
                GLib.idle_add(lambda: self.daemon.pause())
                print("Sync paused (send SIGUSR2 to resume)")

        def handle_resume(signum, frame):
            if self._use_go_daemon and self.daemon_client:
                GLib.idle_add(lambda: self.daemon_client.resume())
                print("Sync resumed")
            elif self.daemon:
                GLib.idle_add(lambda: self.daemon.resume())
                print("Sync resumed")

        signal.signal(signal.SIGUSR1, handle_pause)
        signal.signal(signal.SIGUSR2, handle_resume)

        # Initial status update
        self._update_status()

        Gtk.main()

    def _check_rclone(self) -> bool:
        """Check if rclone is available and configured."""
        # Check rclone is installed
        try:
            result = subprocess.run(
                ['rclone', 'version'],
                capture_output=True,
                timeout=5
            )
            if result.returncode != 0:
                self._show_error(
                    "rclone not found",
                    "Please install rclone: sudo apt install rclone"
                )
                return False
        except (subprocess.SubprocessError, FileNotFoundError):
            self._show_error(
                "rclone not found",
                "Please install rclone: sudo apt install rclone"
            )
            return False

        # Check remote is configured
        try:
            result = subprocess.run(
                ['rclone', 'listremotes'],
                capture_output=True,
                text=True,
                timeout=10
            )
            if f"{self.config.remote_name}:" not in result.stdout:
                self._show_error(
                    "Proton Drive not configured",
                    f"Please configure rclone:\nrclone config\n\n"
                    f"Add a remote named '{self.config.remote_name}'"
                )
                return False
        except subprocess.SubprocessError:
            self._show_error(
                "rclone error",
                "Failed to check rclone configuration"
            )
            return False

        return True


class BisyncSettingsDialog(Gtk.Dialog):
    """Settings dialog for bisync mode."""

    AUTOSTART_DIR = Path.home() / ".config" / "autostart"
    AUTOSTART_FILE = AUTOSTART_DIR / "proton-drive-gtk.desktop"
    DESKTOP_FILE = Path.home() / ".local" / "share" / "applications" / "proton-drive-gtk.desktop"

    def __init__(self, config: Config):
        super().__init__(
            title="Proton Drive Settings",
            flags=Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT
        )
        self.config = config

        self.add_buttons(
            "_Cancel", Gtk.ResponseType.CANCEL,
            "_OK", Gtk.ResponseType.OK
        )

        self.set_default_size(450, 300)
        self.set_modal(True)
        self.set_keep_above(True)

        box = self.get_content_area()
        box.set_spacing(10)
        box.set_margin_start(15)
        box.set_margin_end(15)
        box.set_margin_top(15)
        box.set_margin_bottom(15)

        # Sync folder
        folder_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        folder_label = Gtk.Label(label="Sync folder:")
        folder_label.set_xalign(0)
        folder_label.set_size_request(120, -1)
        self.folder_entry = Gtk.Entry()
        self.folder_entry.set_text(config.mount_path)
        self.folder_entry.set_hexpand(True)
        folder_button = Gtk.Button(label="Browse...")
        folder_button.connect("clicked", self._on_browse_folder)
        folder_box.pack_start(folder_label, False, False, 0)
        folder_box.pack_start(self.folder_entry, True, True, 0)
        folder_box.pack_start(folder_button, False, False, 0)
        box.pack_start(folder_box, False, False, 0)

        # Sync interval
        interval_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        interval_label = Gtk.Label(label="Check interval:")
        interval_label.set_xalign(0)
        interval_label.set_size_request(120, -1)
        self.interval_spin = Gtk.SpinButton.new_with_range(10, 3600, 10)
        self.interval_spin.set_value(config.sync_interval)
        interval_suffix = Gtk.Label(label="seconds")
        interval_box.pack_start(interval_label, False, False, 0)
        interval_box.pack_start(self.interval_spin, False, False, 0)
        interval_box.pack_start(interval_suffix, False, False, 0)
        box.pack_start(interval_box, False, False, 0)

        # Conflict resolution
        conflict_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        conflict_label = Gtk.Label(label="On conflict:")
        conflict_label.set_xalign(0)
        conflict_label.set_size_request(120, -1)
        self.conflict_combo = Gtk.ComboBoxText()
        self.conflict_combo.append("newer", "Keep newer version")
        self.conflict_combo.append("local", "Keep local version")
        self.conflict_combo.append("remote", "Keep remote version")
        self.conflict_combo.set_active_id(config.conflict_resolution)
        conflict_box.pack_start(conflict_label, False, False, 0)
        conflict_box.pack_start(self.conflict_combo, False, False, 0)
        box.pack_start(conflict_box, False, False, 0)

        # Max concurrent transfers
        transfers_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        transfers_label = Gtk.Label(label="Max transfers:")
        transfers_label.set_xalign(0)
        transfers_label.set_size_request(120, -1)
        self.transfers_spin = Gtk.SpinButton.new_with_range(1, 16, 1)
        self.transfers_spin.set_value(config.max_concurrent_transfers)
        transfers_suffix = Gtk.Label(label="concurrent")
        transfers_box.pack_start(transfers_label, False, False, 0)
        transfers_box.pack_start(self.transfers_spin, False, False, 0)
        transfers_box.pack_start(transfers_suffix, False, False, 0)
        box.pack_start(transfers_box, False, False, 0)

        box.pack_start(Gtk.Separator(), False, False, 5)

        # Notifications
        self.notifications_check = Gtk.CheckButton(label="Show notifications")
        self.notifications_check.set_active(config.show_notifications)
        box.pack_start(self.notifications_check, False, False, 0)

        # Start on login - check actual autostart file
        self.start_login_check = Gtk.CheckButton(label="Start on login")
        self.start_login_check.set_active(self._is_autostart_enabled())
        box.pack_start(self.start_login_check, False, False, 0)

        self.show_all()

    def _is_autostart_enabled(self) -> bool:
        """Check if autostart is currently enabled."""
        return self.AUTOSTART_FILE.exists()

    def _enable_autostart(self) -> bool:
        """Enable autostart by creating the autostart desktop file."""
        try:
            self.AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)

            # Copy from installed desktop file or create new one
            if self.DESKTOP_FILE.exists():
                import shutil
                shutil.copy(self.DESKTOP_FILE, self.AUTOSTART_FILE)
            else:
                # Create desktop file from scratch
                script_dir = Path(__file__).parent.parent
                run_script = script_dir / "run.sh"
                content = f"""[Desktop Entry]
Name=Proton Drive
Comment=Sync your files with Proton Drive
Exec={run_script}
Icon=network-server
Terminal=false
Type=Application
Categories=Network;FileTransfer;
StartupNotify=false
X-GNOME-Autostart-enabled=true
"""
                self.AUTOSTART_FILE.write_text(content)
            return True
        except Exception as e:
            print(f"Failed to enable autostart: {e}")
            return False

    def _disable_autostart(self) -> bool:
        """Disable autostart by removing the autostart desktop file."""
        try:
            if self.AUTOSTART_FILE.exists():
                self.AUTOSTART_FILE.unlink()
            return True
        except Exception as e:
            print(f"Failed to disable autostart: {e}")
            return False

    def _on_browse_folder(self, button):
        """Open folder chooser dialog."""
        dialog = Gtk.FileChooserDialog(
            title="Select Sync Folder",
            action=Gtk.FileChooserAction.SELECT_FOLDER
        )
        dialog.add_buttons(
            "_Cancel", Gtk.ResponseType.CANCEL,
            "_Select", Gtk.ResponseType.OK
        )
        dialog.set_current_folder(self.folder_entry.get_text())

        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            self.folder_entry.set_text(dialog.get_filename())
        dialog.destroy()

    def save_config(self):
        """Save the configuration."""
        self.config.mount_path = self.folder_entry.get_text()
        self.config.sync_interval = int(self.interval_spin.get_value())
        self.config.conflict_resolution = self.conflict_combo.get_active_id()
        self.config.show_notifications = self.notifications_check.get_active()
        self.config.max_concurrent_transfers = int(self.transfers_spin.get_value())

        # Handle autostart
        start_on_login = self.start_login_check.get_active()
        if start_on_login and not self._is_autostart_enabled():
            self._enable_autostart()
        elif not start_on_login and self._is_autostart_enabled():
            self._disable_autostart()

        self.config.start_on_login = start_on_login
        self.config.save()


def main():
    """Main entry point for bisync tray."""
    # Ensure single instance
    instance = SingleInstance()
    if not instance.acquire():
        print("Another instance of Proton Drive tray is already running.")
        # Try to show a notification
        try:
            subprocess.run([
                'notify-send',
                'Proton Drive',
                'Another instance is already running.',
                '-i', 'dialog-warning'
            ], timeout=5)
        except Exception:
            pass
        sys.exit(1)

    try:
        tray = BisyncTray()
        tray.run()
    finally:
        instance.release()


if __name__ == "__main__":
    main()
