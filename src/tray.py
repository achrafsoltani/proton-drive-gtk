"""System tray implementation using AppIndicator."""

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('AyatanaAppIndicator3', '0.1')

from gi.repository import Gtk, GLib, AyatanaAppIndicator3 as AppIndicator
import subprocess
import os
from pathlib import Path
from rclone import RcloneManager, MountStatus, TransferStats
from config import Config, get_config


class ProtonDriveTray:
    """System tray application for Proton Drive."""

    ICON_IDLE = "network-offline-symbolic"
    ICON_SYNCING = "network-transmit-receive-symbolic"
    ICON_MOUNTED = "network-idle-symbolic"
    ICON_ERROR = "network-error-symbolic"

    def __init__(self):
        self.config = get_config()
        self.rclone = RcloneManager(self.config)

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

        # Update status periodically
        GLib.timeout_add_seconds(5, self._update_status)

        # Initial status update
        self._update_status()

    def _build_menu(self) -> Gtk.Menu:
        """Build the context menu."""
        menu = Gtk.Menu()

        # Status item (non-clickable)
        self.status_item = Gtk.MenuItem(label="Status: Checking...")
        self.status_item.set_sensitive(False)
        menu.append(self.status_item)

        # Transfer stats (non-clickable)
        self.transfer_item = Gtk.MenuItem(label="")
        self.transfer_item.set_sensitive(False)
        self.transfer_item.set_visible(False)
        menu.append(self.transfer_item)

        menu.append(Gtk.SeparatorMenuItem())

        # Mount/Unmount toggle
        self.mount_item = Gtk.MenuItem(label="Mount")
        self.mount_item.connect("activate", self._on_mount_toggle)
        menu.append(self.mount_item)

        # Pause/Resume toggle
        self.pause_item = Gtk.MenuItem(label="Pause Sync")
        self.pause_item.connect("activate", self._on_pause_toggle)
        self.pause_item.set_sensitive(False)
        menu.append(self.pause_item)

        # Open folder
        self.open_folder_item = Gtk.MenuItem(label="Open Folder")
        self.open_folder_item.connect("activate", self._on_open_folder)
        menu.append(self.open_folder_item)

        menu.append(Gtk.SeparatorMenuItem())

        # Storage info
        self.storage_item = Gtk.MenuItem(label="Storage: --")
        self.storage_item.set_sensitive(False)
        menu.append(self.storage_item)

        menu.append(Gtk.SeparatorMenuItem())

        # Settings
        settings_item = Gtk.MenuItem(label="Settings...")
        settings_item.connect("activate", self._on_settings)
        menu.append(settings_item)

        # Restart
        restart_item = Gtk.MenuItem(label="Restart")
        restart_item.connect("activate", self._on_restart)
        menu.append(restart_item)

        menu.append(Gtk.SeparatorMenuItem())

        # Quit
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", self._on_quit)
        menu.append(quit_item)

        menu.show_all()
        return menu

    def _update_status(self) -> bool:
        """Update the tray status and icon."""
        status = self.rclone.status

        if status == MountStatus.MOUNTED:
            # Check transfer stats
            stats = self.rclone.get_transfer_stats()

            if stats.is_transferring:
                self.indicator.set_icon_full(self.ICON_SYNCING, "Syncing")
                self.status_item.set_label(f"Status: Syncing ({stats.transferring} transfers)")
                self.transfer_item.set_label(f"Speed: {stats.speed_human}")
                self.transfer_item.set_visible(True)
            else:
                self.indicator.set_icon_full(self.ICON_MOUNTED, "Mounted")
                self.status_item.set_label("Status: Mounted")
                self.transfer_item.set_visible(False)

            self.mount_item.set_label("Unmount")
            self.mount_item.set_sensitive(True)
            self.pause_item.set_label("Pause Sync")
            self.pause_item.set_sensitive(True)
            self.open_folder_item.set_sensitive(True)
            self._update_storage_info()

        elif status == MountStatus.PAUSED:
            self.indicator.set_icon_full(self.ICON_IDLE, "Paused")
            self.status_item.set_label("Status: Paused")
            self.transfer_item.set_visible(False)
            self.mount_item.set_label("Unmount")
            self.mount_item.set_sensitive(True)
            self.pause_item.set_label("Resume Sync")
            self.pause_item.set_sensitive(True)
            self.open_folder_item.set_sensitive(True)

        elif status == MountStatus.MOUNTING:
            self.indicator.set_icon_full(self.ICON_SYNCING, "Mounting...")
            self.status_item.set_label("Status: Mounting...")
            self.transfer_item.set_visible(False)
            self.mount_item.set_sensitive(False)
            self.pause_item.set_sensitive(False)

        elif status == MountStatus.ERROR:
            self.indicator.set_icon_full(self.ICON_ERROR, "Error")
            self.status_item.set_label("Status: Error")
            self.transfer_item.set_visible(False)
            self.mount_item.set_label("Mount")
            self.pause_item.set_sensitive(False)

        else:
            self.indicator.set_icon_full(self.ICON_IDLE, "Not mounted")
            self.status_item.set_label("Status: Not mounted")
            self.transfer_item.set_visible(False)
            self.mount_item.set_label("Mount")
            self.pause_item.set_sensitive(False)
            self.open_folder_item.set_sensitive(False)

        return True  # Continue the timeout

    def _update_storage_info(self):
        """Update storage information."""
        info = self.rclone.get_remote_info()
        if info:
            used = info.get("used", 0)
            total = info.get("total", 0)
            if total > 0:
                used_gb = used / (1024 ** 3)
                total_gb = total / (1024 ** 3)
                self.storage_item.set_label(f"Storage: {used_gb:.1f} / {total_gb:.1f} GB")
                return
        self.storage_item.set_label("Storage: --")

    def _on_mount_toggle(self, widget):
        """Handle mount/unmount toggle."""
        if self.rclone.status in (MountStatus.MOUNTED, MountStatus.PAUSED):
            success, message = self.rclone.unmount()
            if not success:
                self._show_error("Unmount Failed", message)
        else:
            success, message = self.rclone.mount()
            if not success:
                self._show_error("Mount Failed", message)

        self._update_status()

    def _on_pause_toggle(self, widget):
        """Handle pause/resume toggle."""
        if self.rclone.is_paused:
            success, message = self.rclone.resume()
            if not success:
                self._show_error("Resume Failed", message)
        else:
            success, message = self.rclone.pause()
            if not success:
                self._show_error("Pause Failed", message)

        self._update_status()

    def _on_open_folder(self, widget):
        """Open the mount folder in file manager."""
        mount_path = self.config.mount_path
        if Path(mount_path).exists():
            subprocess.Popen(["xdg-open", mount_path])

    def _on_settings(self, widget):
        """Open settings dialog."""
        GLib.idle_add(self._show_settings_dialog)

    def _show_settings_dialog(self):
        """Show settings dialog (called from idle)."""
        dialog = SettingsDialog(self.config)
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            dialog.save_config()
            self.config = get_config()
            self.rclone = RcloneManager(self.config)
        dialog.destroy()
        return False  # Don't repeat

    def _on_restart(self, widget):
        """Restart the application."""
        import sys
        import os

        # Get the script path
        script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        run_script = os.path.join(script_dir, "run.sh")

        # Start new instance before quitting
        subprocess.Popen(
            [run_script],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        # Quit current instance
        Gtk.main_quit()

    def _on_quit(self, widget):
        """Quit the application."""
        # Unmount before quitting
        if self.rclone.status == MountStatus.MOUNTED:
            self.rclone.unmount()
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

    def run(self):
        """Start the application."""
        # Check prerequisites
        if not self.rclone.is_rclone_available():
            self._show_error(
                "rclone not found",
                "Please install rclone: sudo apt install rclone"
            )
            return

        if not self.rclone.is_remote_configured():
            self._show_error(
                "Proton Drive not configured",
                f"Please configure rclone:\nrclone config\n\nAdd a remote named '{self.config.remote_name}'"
            )
            return

        # Auto-mount if enabled
        if self.config.auto_mount:
            self.rclone.mount()

        Gtk.main()


class SettingsDialog(Gtk.Dialog):
    """Settings dialog."""

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

        self.set_default_size(400, 200)
        self.set_modal(True)
        self.set_keep_above(True)

        box = self.get_content_area()
        box.set_spacing(10)
        box.set_margin_start(10)
        box.set_margin_end(10)
        box.set_margin_top(10)
        box.set_margin_bottom(10)

        # Mount path
        path_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        path_label = Gtk.Label(label="Mount path:")
        self.path_entry = Gtk.Entry()
        self.path_entry.set_text(config.mount_path)
        self.path_entry.set_hexpand(True)
        path_box.pack_start(path_label, False, False, 0)
        path_box.pack_start(self.path_entry, True, True, 0)
        box.pack_start(path_box, False, False, 0)

        # Auto-mount
        self.auto_mount_check = Gtk.CheckButton(label="Auto-mount on startup")
        self.auto_mount_check.set_active(config.auto_mount)
        box.pack_start(self.auto_mount_check, False, False, 0)

        # Notifications
        self.notifications_check = Gtk.CheckButton(label="Show notifications")
        self.notifications_check.set_active(config.show_notifications)
        box.pack_start(self.notifications_check, False, False, 0)

        self.show_all()

    def save_config(self):
        """Save the configuration."""
        self.config.mount_path = self.path_entry.get_text()
        self.config.auto_mount = self.auto_mount_check.get_active()
        self.config.show_notifications = self.notifications_check.get_active()
        self.config.save()
