"""Sync daemon using rclone operations for reliable synchronisation.

This module provides the core sync engine that manages bidirectional
synchronisation between a local folder and Proton Drive using individual
rclone operations (lsjson, copyto, deletefile) to avoid the crash bug
in rclone's bulk sync operations with Proton Drive.
"""

import json
import logging
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Set

from .state_db import StateDatabase, SyncStatus, FileState
from .file_watcher import FileWatcher


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class DaemonStatus(Enum):
    """Status of the sync daemon."""
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    SYNCING = "syncing"
    PAUSED = "paused"
    ERROR = "error"


@dataclass
class RemoteFile:
    """Information about a remote file."""
    path: str
    name: str
    size: int
    mod_time: float  # Unix timestamp
    is_dir: bool
    mime_type: str = ""
    file_id: str = ""

    def is_proton_doc(self) -> bool:
        """Check if this is a native Proton Doc (not downloadable).

        Native Proton Docs crash rclone when trying to download them
        because they don't have regular file content.
        """
        return self.mime_type == "application/vnd.proton.doc"


# MIME types that cannot be downloaded (crash rclone)
UNSUPPORTED_MIME_TYPES = {
    "application/vnd.proton.doc",  # Native Proton Docs
}


@dataclass
class SyncStats:
    """Statistics about current sync state."""
    status: DaemonStatus
    total_files: int = 0
    synced_files: int = 0
    pending_upload: int = 0
    pending_download: int = 0
    syncing_files: int = 0
    conflicts: int = 0
    errors: int = 0
    last_sync: Optional[float] = None
    current_file: Optional[str] = None
    speed: Optional[str] = None
    # Listing progress
    listing_dirs: int = 0
    listing_files: int = 0
    listing_queued: int = 0
    is_listing: bool = False
    # Download/upload progress
    download_total: int = 0
    download_done: int = 0
    upload_total: int = 0
    upload_done: int = 0
    is_downloading: bool = False
    is_uploading: bool = False
    eta_seconds: Optional[int] = None


class SyncDaemon:
    """Main sync daemon using individual rclone operations.

    This daemon manages bidirectional synchronisation between a local folder
    and Proton Drive remote using individual rclone commands to avoid
    the crash bug in bulk sync operations.
    """

    # Sync debounce time in seconds
    SYNC_DEBOUNCE = 5.0

    # Remote check interval in seconds
    REMOTE_CHECK_INTERVAL = 60.0

    # Timeout for individual operations
    OPERATION_TIMEOUT = 300  # 5 minutes per file

    # Number of parallel downloads (be conservative with Proton Drive API)
    PARALLEL_DOWNLOADS = 4

    # Number of parallel directory listings
    PARALLEL_LISTINGS = 3

    # Retry settings
    MAX_RETRIES = 3
    RETRY_DELAY = 5  # Base delay in seconds (exponential backoff)

    def __init__(
        self,
        local_path: str,
        remote_name: str = "protondrive",
        db_path: Optional[Path] = None,
        on_status_change: Optional[Callable[[DaemonStatus], None]] = None,
        on_sync_progress: Optional[Callable[[str, int, int], None]] = None
    ):
        """Initialise the sync daemon.

        Args:
            local_path: Path to the local sync folder (e.g., ~/ProtonDrive)
            remote_name: Name of the rclone remote
            db_path: Path to the state database
            on_status_change: Callback when daemon status changes
            on_sync_progress: Callback for sync progress (file, done, total)
        """
        self.local_path = Path(local_path).expanduser().resolve()
        self.remote_name = remote_name
        self.remote = f"{remote_name}:"

        # State database (also handles remote file caching)
        self.db = StateDatabase(db_path)

        # Callbacks
        self._on_status_change = on_status_change
        self._on_sync_progress = on_sync_progress

        # Internal state
        self._status = DaemonStatus.STOPPED
        self._lock = threading.Lock()
        self._sync_timer: Optional[threading.Timer] = None
        self._remote_check_timer: Optional[threading.Timer] = None
        self._running = False
        self._paused = False
        self._current_file: Optional[str] = None
        self._last_sync: Optional[float] = None
        self._sync_thread: Optional[threading.Thread] = None
        # Listing progress tracking
        self._listing_dirs = 0
        self._listing_files = 0
        self._listing_queued = 0
        self._is_listing = False
        # Download/upload progress tracking
        self._download_total = 0
        self._download_done = 0
        self._upload_total = 0
        self._upload_done = 0
        self._is_downloading = False
        self._is_uploading = False
        self._sync_start_time: Optional[float] = None
        self._files_per_second = 0.0

        # File watcher for real-time sync
        self._file_watcher: Optional[FileWatcher] = None
        self._pending_sync = False

    @property
    def status(self) -> DaemonStatus:
        """Get current daemon status."""
        return self._status

    def _set_status(self, status: DaemonStatus) -> None:
        """Set daemon status and notify callback."""
        if self._status != status:
            self._status = status
            if self._on_status_change:
                self._on_status_change(status)

    def _save_file_list_cache(self, files: List[RemoteFile], downloaded: Set[str], listing_complete: bool = False) -> None:
        """Save the file list and download progress to database."""
        try:
            file_dicts = [
                {
                    "path": f.path,
                    "name": f.name,
                    "size": f.size,
                    "mod_time": f.mod_time,
                    "mime_type": f.mime_type,
                    "file_id": f.file_id
                }
                for f in files
            ]
            self.db.save_remote_files(file_dicts, mark_listing_complete=listing_complete)

            # Mark downloaded files
            for path in downloaded:
                self.db.mark_remote_file_downloaded(path)

            logger.debug(f"Saved file list to DB: {len(files)} files, {len(downloaded)} downloaded")
        except Exception as e:
            logger.error(f"Failed to save file list cache: {e}")

    def _load_file_list_cache(self) -> Optional[Tuple[List[RemoteFile], Set[str]]]:
        """Load the file list cache from database.

        Returns:
            Tuple of (file list, set of downloaded paths) or None if cache invalid/missing
        """
        try:
            cached = self.db.get_remote_files()
            if not cached:
                # Try to seed cache from sync_history (recovery from old sessions)
                seeded = self.db.seed_cache_from_history(str(self.local_path))
                if seeded > 0:
                    logger.info(f"Seeded cache from history: {seeded} downloaded files")
                    cached = self.db.get_remote_files()
                if not cached:
                    return None

            files = [
                RemoteFile(
                    path=item["path"],
                    name=item["name"],
                    size=item["size"],
                    mod_time=item["mod_time"],
                    is_dir=False,
                    mime_type=item.get("mime_type", ""),
                    file_id=item.get("file_id", "")
                )
                for item in cached
            ]
            downloaded = {item["path"] for item in cached if item.get("downloaded", 0) == 1}

            logger.info(f"Loaded file list from DB: {len(files)} files, {len(downloaded)} already downloaded")
            return files, downloaded

        except Exception as e:
            logger.error(f"Failed to load file list cache: {e}")
            return None

    def _clear_file_list_cache(self) -> None:
        """Clear the file list cache from database."""
        try:
            self.db.clear_remote_files_cache()
            logger.info("Cleared file list cache")
        except Exception as e:
            logger.error(f"Failed to clear file list cache: {e}")

    def get_stats(self) -> SyncStats:
        """Get current sync statistics."""
        db_stats = self.db.get_stats()

        # Calculate ETA
        eta_seconds = None
        if self._is_downloading and self._files_per_second > 0:
            remaining = self._download_total - self._download_done
            eta_seconds = int(remaining / self._files_per_second)
        elif self._is_uploading and self._files_per_second > 0:
            remaining = self._upload_total - self._upload_done
            eta_seconds = int(remaining / self._files_per_second)

        return SyncStats(
            status=self._status,
            total_files=sum(db_stats.values()),
            synced_files=db_stats.get('synced', 0),
            pending_upload=db_stats.get('pending_upload', 0),
            pending_download=db_stats.get('pending_download', 0),
            syncing_files=db_stats.get('syncing', 0),
            conflicts=db_stats.get('conflict', 0),
            errors=db_stats.get('error', 0),
            last_sync=self._last_sync,
            current_file=self._current_file,
            listing_dirs=self._listing_dirs,
            listing_files=self._listing_files,
            listing_queued=self._listing_queued,
            is_listing=self._is_listing,
            download_total=self._download_total,
            download_done=self._download_done,
            upload_total=self._upload_total,
            upload_done=self._upload_done,
            is_downloading=self._is_downloading,
            is_uploading=self._is_uploading,
            eta_seconds=eta_seconds
        )

    def is_rclone_available(self) -> bool:
        """Check if rclone is installed."""
        try:
            result = subprocess.run(
                ['rclone', 'version'],
                capture_output=True,
                timeout=5
            )
            return result.returncode == 0
        except (subprocess.SubprocessError, FileNotFoundError):
            return False

    def is_remote_configured(self) -> bool:
        """Check if the remote is configured in rclone."""
        try:
            result = subprocess.run(
                ['rclone', 'listremotes'],
                capture_output=True,
                text=True,
                timeout=10
            )
            return f"{self.remote_name}:" in result.stdout
        except subprocess.SubprocessError:
            return False

    def start(self) -> Tuple[bool, str]:
        """Start the sync daemon.

        Returns:
            Tuple of (success, message)
        """
        with self._lock:
            if self._running:
                return True, "Daemon already running"

            # Verify prerequisites
            if not self.is_rclone_available():
                return False, "rclone is not installed"

            if not self.is_remote_configured():
                return False, f"Remote '{self.remote_name}' is not configured"

            # Ensure local folder exists
            self.local_path.mkdir(parents=True, exist_ok=True)

            self._running = True
            self._set_status(DaemonStatus.STARTING)

        # Start file watcher for real-time sync
        self._file_watcher = FileWatcher(str(self.local_path), self.on_local_change)
        if self._file_watcher.start():
            logger.info("File watcher started for real-time sync")
        else:
            logger.warning("File watcher failed to start, using polling only")

        # Start initial sync in background
        self._sync_thread = threading.Thread(target=self._initial_sync, daemon=True)
        self._sync_thread.start()

        return True, "Daemon started"

    def stop(self) -> Tuple[bool, str]:
        """Stop the sync daemon.

        Returns:
            Tuple of (success, message)
        """
        with self._lock:
            if not self._running:
                return True, "Daemon not running"

            self._running = False

            # Stop file watcher
            if self._file_watcher:
                self._file_watcher.stop()
                self._file_watcher = None

            # Cancel timers
            if self._sync_timer:
                self._sync_timer.cancel()
                self._sync_timer = None

            if self._remote_check_timer:
                self._remote_check_timer.cancel()
                self._remote_check_timer = None

            self._set_status(DaemonStatus.STOPPED)

        return True, "Daemon stopped"

    def pause(self) -> Tuple[bool, str]:
        """Pause synchronisation."""
        with self._lock:
            if not self._running:
                return False, "Daemon not running"

            self._paused = True

            if self._sync_timer:
                self._sync_timer.cancel()
                self._sync_timer = None

            if self._remote_check_timer:
                self._remote_check_timer.cancel()
                self._remote_check_timer = None

            self._set_status(DaemonStatus.PAUSED)

        return True, "Sync paused"

    def resume(self) -> Tuple[bool, str]:
        """Resume synchronisation."""
        with self._lock:
            if not self._running:
                return False, "Daemon not running"

            self._paused = False
            self._set_status(DaemonStatus.RUNNING)

        # Trigger immediate sync (will use cache)
        self._sync_thread = threading.Thread(target=self._resume_sync, daemon=True)
        self._sync_thread.start()

        return True, "Sync resumed"

    def _resume_sync(self) -> None:
        """Resume sync after pause - uses cache to avoid re-listing."""
        logger.info("Resuming sync from cache...")
        success = self._run_sync()
        if success:
            self._set_status(DaemonStatus.RUNNING)
            self._schedule_remote_check()
        else:
            self._set_status(DaemonStatus.ERROR)

    # =========================================================================
    # Remote Operations (using individual rclone commands)
    # =========================================================================

    def _list_remote(self, path: str = "", recursive: bool = False) -> List[RemoteFile]:
        """List files on remote using rclone lsjson.

        Includes retry with exponential backoff for transient failures.

        Args:
            path: Remote path to list (relative to remote root)
            recursive: If True, list recursively (can be slow)

        Returns:
            List of RemoteFile objects
        """
        remote_path = f"{self.remote}{path}"

        cmd = ['rclone', 'lsjson', remote_path]
        if recursive:
            cmd.append('-R')
        # Don't use --no-modtime as we need ModTime for sync decisions

        for attempt in range(self.MAX_RETRIES):
            if not self._running or self._paused:
                return []

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=300 if recursive else 60
                )

                if result.returncode != 0:
                    error_msg = result.stderr.lower()
                    if any(err in error_msg for err in ['timeout', 'connection', 'network', 'temporary', 'unavailable']):
                        delay = self.RETRY_DELAY * (2 ** attempt)
                        logger.warning(f"Retry {attempt + 1}/{self.MAX_RETRIES} listing {path or '/'} in {delay}s: {result.stderr[:100]}")
                        time.sleep(delay)
                        continue
                    logger.error(f"Failed to list remote: {result.stderr}")
                    return []

                files = []
                for item in json.loads(result.stdout):
                    # Parse modification time
                    mod_time = 0.0
                    if 'ModTime' in item:
                        try:
                            from datetime import datetime
                            dt = datetime.fromisoformat(item['ModTime'].replace('Z', '+00:00'))
                            mod_time = dt.timestamp()
                        except (ValueError, KeyError):
                            pass

                    files.append(RemoteFile(
                        path=item.get('Path', ''),
                        name=item.get('Name', ''),
                        size=item.get('Size', 0),
                        mod_time=mod_time,
                        is_dir=item.get('IsDir', False),
                        mime_type=item.get('MimeType', ''),
                        file_id=item.get('ID', '')
                    ))

                return files

            except subprocess.TimeoutExpired:
                delay = self.RETRY_DELAY * (2 ** attempt)
                logger.warning(f"Retry {attempt + 1}/{self.MAX_RETRIES} listing {path or '/'} in {delay}s: timeout")
                time.sleep(delay)
                continue
            except (json.JSONDecodeError, subprocess.SubprocessError) as e:
                delay = self.RETRY_DELAY * (2 ** attempt)
                logger.warning(f"Retry {attempt + 1}/{self.MAX_RETRIES} listing {path or '/'} in {delay}s: {e}")
                time.sleep(delay)
                continue

        logger.error(f"Failed to list {path or '/'} after {self.MAX_RETRIES} retries")
        return []

    def _download_file(self, remote_path: str, local_path: Path) -> bool:
        """Download a single file from remote using rclone cat.

        Uses 'rclone cat' which is more reliable than 'rclone copy' for
        Proton Drive as it avoids the nil pointer crash in the backend.
        Includes retry with exponential backoff for transient failures.

        Args:
            remote_path: Path on remote (relative to remote root)
            local_path: Local destination path

        Returns:
            True if successful
        """
        self._current_file = remote_path
        self.db.mark_syncing(str(local_path))

        # Ensure parent directory exists
        local_path.parent.mkdir(parents=True, exist_ok=True)

        last_error = ""
        for attempt in range(self.MAX_RETRIES):
            if not self._running or self._paused:
                return False

            try:
                # Use rclone cat to download file content
                with open(local_path, 'wb') as f:
                    process = subprocess.Popen(
                        ['rclone', 'cat', f'{self.remote}{remote_path}'],
                        stdout=f,
                        stderr=subprocess.PIPE
                    )
                    _, stderr = process.communicate(timeout=self.OPERATION_TIMEOUT)

                    if process.returncode == 0:
                        # Update database
                        stat = local_path.stat()
                        self.db.mark_synced(str(local_path), stat.st_mtime, stat.st_size)
                        self.db.log_sync_action('download', str(local_path), 'success')
                        logger.info(f"Downloaded: {remote_path}")
                        return True
                    else:
                        last_error = stderr.decode('utf-8', errors='replace')[:200]
                        # Check if it's a retryable error
                        if any(err in last_error.lower() for err in ['timeout', 'connection', 'network', 'temporary', 'unavailable']):
                            delay = self.RETRY_DELAY * (2 ** attempt)
                            logger.warning(f"Retry {attempt + 1}/{self.MAX_RETRIES} for {remote_path} in {delay}s: {last_error}")
                            local_path.unlink(missing_ok=True)
                            time.sleep(delay)
                            continue
                        else:
                            # Non-retryable error
                            break

            except subprocess.TimeoutExpired:
                last_error = "Download timeout"
                local_path.unlink(missing_ok=True)
                delay = self.RETRY_DELAY * (2 ** attempt)
                logger.warning(f"Retry {attempt + 1}/{self.MAX_RETRIES} for {remote_path} in {delay}s: timeout")
                time.sleep(delay)
                continue
            except (subprocess.SubprocessError, OSError) as e:
                last_error = str(e)[:200]
                local_path.unlink(missing_ok=True)
                delay = self.RETRY_DELAY * (2 ** attempt)
                logger.warning(f"Retry {attempt + 1}/{self.MAX_RETRIES} for {remote_path} in {delay}s: {e}")
                time.sleep(delay)
                continue

        # All retries exhausted
        self.db.mark_error(str(local_path), last_error)
        self.db.log_sync_action('download', str(local_path), 'failed')
        logger.error(f"Failed to download {remote_path} after {self.MAX_RETRIES} retries: {last_error}")
        local_path.unlink(missing_ok=True)
        self._current_file = None
        return False

    def _upload_file(self, local_path: Path, remote_path: str) -> bool:
        """Upload a single file to remote.

        Args:
            local_path: Local source path
            remote_path: Path on remote (relative to remote root)

        Returns:
            True if successful
        """
        self._current_file = str(local_path)
        self.db.mark_syncing(str(local_path))

        try:
            result = subprocess.run(
                ['rclone', 'copyto', '--protondrive-replace-existing-draft=true',
                 str(local_path), f'{self.remote}{remote_path}'],
                capture_output=True,
                text=True,
                timeout=self.OPERATION_TIMEOUT
            )

            if result.returncode == 0:
                stat = local_path.stat()
                self.db.mark_synced(str(local_path), stat.st_mtime, stat.st_size)
                self.db.log_sync_action('upload', str(local_path), 'success')
                logger.info(f"Uploaded: {local_path.name}")
                return True
            else:
                # Check if file already exists on remote - treat as success
                if 'already exists' in result.stderr.lower():
                    stat = local_path.stat()
                    self.db.mark_synced(str(local_path), stat.st_mtime, stat.st_size)
                    self.db.log_sync_action('upload', str(local_path), 'success')
                    logger.info(f"Already exists on remote: {local_path.name}")
                    return True
                self.db.mark_error(str(local_path), result.stderr[:200])
                self.db.log_sync_action('upload', str(local_path), 'failed')
                logger.error(f"Failed to upload {local_path}: {result.stderr}")
                return False

        except subprocess.TimeoutExpired:
            self.db.mark_error(str(local_path), "Upload timeout")
            logger.error(f"Timeout uploading: {local_path}")
            return False
        except subprocess.SubprocessError as e:
            self.db.mark_error(str(local_path), str(e)[:200])
            logger.error(f"Error uploading {local_path}: {e}")
            return False
        finally:
            self._current_file = None

    def _delete_remote(self, remote_path: str) -> bool:
        """Delete a file from remote.

        Args:
            remote_path: Path on remote to delete

        Returns:
            True if successful
        """
        try:
            result = subprocess.run(
                ['rclone', 'deletefile', f'{self.remote}{remote_path}'],
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0:
                self.db.log_sync_action('delete_remote', remote_path, 'success')
                logger.info(f"Deleted from remote: {remote_path}")
                return True
            else:
                logger.error(f"Failed to delete remote {remote_path}: {result.stderr}")
                return False

        except subprocess.SubprocessError as e:
            logger.error(f"Error deleting remote {remote_path}: {e}")
            return False

    def _create_remote_dir(self, remote_path: str) -> bool:
        """Create a directory on remote.

        Args:
            remote_path: Path on remote to create

        Returns:
            True if successful
        """
        try:
            result = subprocess.run(
                ['rclone', 'mkdir', f'{self.remote}{remote_path}'],
                capture_output=True,
                text=True,
                timeout=30
            )
            return result.returncode == 0
        except subprocess.SubprocessError:
            return False

    # =========================================================================
    # Sync Logic
    # =========================================================================

    def _initial_sync(self) -> None:
        """Perform initial synchronisation."""
        logger.info("Starting initial sync...")

        # Check if this is truly the first sync (local folder empty or nearly empty)
        local_files = list(self.local_path.glob('**/*'))
        local_file_count = sum(1 for f in local_files if f.is_file())

        if local_file_count < 5:
            # Use rclone copy for initial bulk download (faster than individual operations)
            logger.info("Performing bulk initial download...")
            success = self._bulk_download()
        else:
            # Incremental sync
            logger.info("Performing incremental sync...")
            success = self._run_sync()

        if success:
            self._set_status(DaemonStatus.RUNNING)
            self._schedule_remote_check()
            logger.info("Initial sync completed successfully")
        else:
            self._set_status(DaemonStatus.ERROR)
            logger.error("Initial sync failed")

    def _bulk_download(self) -> bool:
        """Perform bulk download by listing remote and downloading each file.

        Uses 'rclone cat' for each file which is more reliable than
        'rclone copy' for Proton Drive. Downloads are parallelised for speed.
        Supports resuming from cache if interrupted.

        Returns:
            True if successful
        """
        self._set_status(DaemonStatus.SYNCING)

        try:
            logger.info(f"Downloading from {self.remote} to {self.local_path}")

            # Try to load from cache first
            cached = self._load_file_list_cache()
            if cached:
                remote_files, downloaded = cached
                logger.info(f"Resuming from cache: {len(downloaded)}/{len(remote_files)} already downloaded")
            else:
                # List all remote files
                remote_files = self._list_remote_recursive()
                downloaded: Set[str] = set()
                # Save cache immediately after listing
                self._save_file_list_cache(remote_files, downloaded, listing_complete=True)

            total = len(remote_files)
            # Filter out already downloaded files
            to_download = [rf for rf in remote_files if rf.path not in downloaded]
            already_done = len(downloaded)
            completed = already_done
            errors = 0
            completed_lock = threading.Lock()

            logger.info(f"Found {total} files total, {len(to_download)} remaining (using {self.PARALLEL_DOWNLOADS} parallel downloads)")

            def download_task(rf: RemoteFile) -> Tuple[str, bool]:
                """Download a single file and return result."""
                if not self._running or self._paused:
                    return rf.path, False
                local_path = self.local_path / rf.path
                success = self._download_file(rf.path, local_path)
                return rf.path, success

            with ThreadPoolExecutor(max_workers=self.PARALLEL_DOWNLOADS) as executor:
                # Submit all download tasks
                futures = {executor.submit(download_task, rf): rf for rf in to_download}

                for future in as_completed(futures):
                    if not self._running or self._paused:
                        # Save progress before stopping
                        self._save_file_list_cache(remote_files, downloaded, listing_complete=True)
                        # Cancel remaining futures
                        for f in futures:
                            f.cancel()
                        break

                    try:
                        path, success = future.result()
                        with completed_lock:
                            completed += 1
                            if success:
                                downloaded.add(path)
                            else:
                                errors += 1

                            if self._on_sync_progress:
                                self._on_sync_progress(path, completed, total)

                            # Log progress and save cache every 10 files
                            if completed % 10 == 0:
                                logger.info(f"Progress: {completed}/{total} files ({errors} errors)")
                                self._save_file_list_cache(remote_files, downloaded, listing_complete=True)

                    except Exception as e:
                        with completed_lock:
                            completed += 1
                            errors += 1
                        logger.error(f"Download task error: {e}")

            self._current_file = None
            self._last_sync = time.time()

            # Clear cache if all files downloaded successfully
            if len(downloaded) == total:
                self._clear_file_list_cache()
                logger.info(f"Bulk download completed: {completed} files, {errors} errors")
            else:
                # Save final state for resume
                self._save_file_list_cache(remote_files, downloaded, listing_complete=True)
                logger.info(f"Bulk download interrupted: {completed}/{total} files, {errors} errors")

            return errors == 0

        except Exception as e:
            logger.error(f"Bulk download error: {e}")
            return False

    def _bulk_upload(self) -> bool:
        """Perform bulk upload using rclone copy with single transfer.

        Returns:
            True if successful
        """
        self._set_status(DaemonStatus.SYNCING)

        try:
            logger.info(f"Uploading from {self.local_path} to {self.remote}")

            process = subprocess.Popen(
                [
                    'rclone', 'copy',
                    str(self.local_path),
                    self.remote,
                    '--transfers=1',
                    '--checkers=1',
                    '--protondrive-replace-existing-draft=true',
                    '-v',
                    '--stats', '5s'
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )

            for line in process.stdout:
                line = line.strip()
                if line:
                    if ': Copied' in line or ': Copying' in line:
                        parts = line.split(':')
                        if len(parts) >= 2:
                            self._current_file = parts[1].strip().split()[0]
                    logger.debug(line)

            return_code = process.wait()
            self._current_file = None

            if return_code == 0:
                logger.info("Bulk upload completed")
                self._update_db_from_local()
                self._last_sync = time.time()
                return True
            else:
                logger.error(f"Bulk upload failed with code {return_code}")
                return False

        except subprocess.SubprocessError as e:
            logger.error(f"Bulk upload error: {e}")
            return False

    def _update_db_from_local(self) -> None:
        """Update the database from local files after bulk sync."""
        logger.info("Updating database from local files...")
        count = 0
        for root, dirs, files in os.walk(self.local_path):
            # Skip hidden directories
            dirs[:] = [d for d in dirs if not d.startswith('.')]

            for name in files:
                if name.startswith('.'):
                    continue

                file_path = Path(root) / name
                try:
                    stat = file_path.stat()
                    self.db.mark_synced(str(file_path), stat.st_mtime, stat.st_size)
                    count += 1
                except OSError:
                    pass

        logger.info(f"Database updated: {count} files")

    def _list_remote_recursive(self, path: str = "") -> List[RemoteFile]:
        """List remote files recursively by walking directories in parallel.

        Uses parallel directory listing for faster performance while being
        reliable and not timing out on large directories.

        Args:
            path: Starting path (relative to remote root)

        Returns:
            List of all RemoteFile objects (files only, not directories)
        """
        all_files: List[RemoteFile] = []
        dirs_to_process = [path]
        files_lock = threading.Lock()
        dirs_lock = threading.Lock()

        self._is_listing = True
        self._listing_dirs = 0
        self._listing_files = 0
        self._listing_queued = 0

        dir_count = 0

        def list_directory(current_dir: str) -> Tuple[List[RemoteFile], List[str]]:
            """List a single directory and return files and subdirs."""
            if not self._running or self._paused:
                return [], []

            items = self._list_remote(current_dir)
            files = []
            subdirs = []

            for item in items:
                if item.is_dir:
                    # Skip hidden directories
                    if item.name.startswith('.'):
                        continue
                    subdir = f"{current_dir}/{item.name}" if current_dir else item.name
                    subdirs.append(subdir)
                else:
                    # Skip unsupported file types
                    if item.mime_type in UNSUPPORTED_MIME_TYPES:
                        continue
                    # Set full path
                    if current_dir:
                        item.path = f"{current_dir}/{item.name}"
                    files.append(item)

            return files, subdirs

        with ThreadPoolExecutor(max_workers=self.PARALLEL_LISTINGS) as executor:
            while dirs_to_process and self._running and not self._paused:
                # Take a batch of directories to process in parallel
                batch_size = min(self.PARALLEL_LISTINGS, len(dirs_to_process))
                batch = []
                with dirs_lock:
                    for _ in range(batch_size):
                        if dirs_to_process:
                            batch.append(dirs_to_process.pop(0))

                if not batch:
                    break

                # Submit all directories in batch
                futures = {executor.submit(list_directory, d): d for d in batch}

                for future in as_completed(futures):
                    if not self._running or self._paused:
                        break

                    try:
                        files, subdirs = future.result()
                        with files_lock:
                            all_files.extend(files)
                        with dirs_lock:
                            dirs_to_process.extend(subdirs)
                            dir_count += 1

                            # Update tracking variables
                            self._listing_dirs = dir_count
                            self._listing_files = len(all_files)
                            self._listing_queued = len(dirs_to_process)

                            if dir_count % 10 == 0:
                                logger.info(f"Listing progress: {dir_count} directories scanned, {len(all_files)} files found, {len(dirs_to_process)} dirs queued")
                    except Exception as e:
                        logger.error(f"Error listing directory: {e}")

        self._is_listing = False
        return all_files

    def _run_sync(self) -> bool:
        """Run a full sync cycle.

        Returns:
            True if sync completed successfully
        """
        if self._paused or not self._running:
            return True

        self._set_status(DaemonStatus.SYNCING)

        try:
            # Try to load from cache first
            cached = self._load_file_list_cache()
            if cached:
                remote_files, downloaded = cached
                logger.info(f"Using cached file list: {len(remote_files)} files, {len(downloaded)} already downloaded")
            else:
                # Get remote file list (walk directories incrementally)
                logger.info("Fetching remote file list...")
                remote_files = self._list_remote_recursive()
                downloaded = set()

                # Check if we were paused during listing - don't save partial cache
                if self._paused or not self._running:
                    logger.info("Sync paused during listing, will re-list on resume")
                    return True

                # Save cache after complete listing
                if remote_files:
                    self._save_file_list_cache(remote_files, downloaded, listing_complete=True)
                    logger.info(f"Saved file list cache: {len(remote_files)} files")

            # Check if we were paused - shouldn't happen here but just in case
            if self._paused or not self._running:
                return True

            if not remote_files and self._is_remote_empty():
                logger.info("Remote is empty, nothing to sync")
                self._last_sync = time.time()
                return True

            logger.info(f"Found {len(remote_files)} remote files")

            # Build remote file map (path -> RemoteFile)
            remote_map: Dict[str, RemoteFile] = {}
            for rf in remote_files:
                remote_map[rf.path] = rf

            # Get local file list
            logger.info("Scanning local files...")
            local_files = self._scan_local_files()

            # Build local file map (relative path -> absolute path)
            local_map: Dict[str, Path] = {}
            for lf in local_files:
                rel_path = lf.relative_to(self.local_path)
                local_map[str(rel_path)] = lf

            logger.info(f"Found {len(local_files)} local files")

            # Determine what needs to be synced
            to_download: List[RemoteFile] = []
            to_upload: List[Path] = []

            # Check for files to download (on remote but not local, or remote is newer)
            for remote_path, rf in remote_map.items():
                local_path = self.local_path / remote_path

                if remote_path not in local_map:
                    # File exists on remote but not locally
                    to_download.append(rf)
                else:
                    # File exists both places - check if remote is newer
                    try:
                        local_stat = local_path.stat()
                    except OSError:
                        to_download.append(rf)
                        continue

                    db_state = self.db.get_file_state(str(local_path))

                    if db_state is None:
                        # Not in DB, compare timestamps
                        if rf.mod_time > local_stat.st_mtime + 1:  # 1 second tolerance
                            to_download.append(rf)
                    elif db_state.status == SyncStatus.PENDING_UPLOAD:
                        # Local changes pending - don't overwrite
                        pass
                    elif rf.size != local_stat.st_size:
                        # Size mismatch - download remote version
                        to_download.append(rf)

            # Check for files to upload (local but not on remote, or local is newer)
            for rel_path, local_path in local_map.items():
                if rel_path not in remote_map:
                    # File exists locally but not on remote
                    to_upload.append(local_path)
                else:
                    # Check if local is newer
                    rf = remote_map[rel_path]
                    try:
                        local_stat = local_path.stat()
                    except OSError:
                        continue

                    db_state = self.db.get_file_state(str(local_path))

                    if db_state and db_state.status == SyncStatus.PENDING_UPLOAD:
                        to_upload.append(local_path)
                    elif local_stat.st_mtime > rf.mod_time + 1:  # 1 second tolerance
                        # Local is newer
                        to_upload.append(local_path)

            # Filter out already downloaded files from cache
            to_download = [rf for rf in to_download if rf.path not in downloaded]

            # Filter out files that exist on remote from uploads (they don't need uploading)
            # Also filter out files that were just downloaded in this session
            to_upload = [p for p in to_upload if str(p.relative_to(self.local_path)) not in remote_map]
            to_upload = [p for p in to_upload if str(p.relative_to(self.local_path)) not in downloaded]

            logger.info(f"Sync plan: {len(to_download)} downloads, {len(to_upload)} uploads (skipping {len(downloaded)} cached)")

            completed_lock = threading.Lock()

            # Download files in parallel
            if to_download:
                self._download_total = len(to_download)
                self._download_done = 0
                self._is_downloading = True
                self._sync_start_time = time.time()

                def download_task(rf: RemoteFile) -> Tuple[str, bool]:
                    if not self._running or self._paused:
                        return rf.path, False
                    local_path = self.local_path / rf.path
                    self.db.mark_pending_download(str(local_path))
                    success = self._download_file(rf.path, local_path)
                    return rf.path, success

                with ThreadPoolExecutor(max_workers=self.PARALLEL_DOWNLOADS) as executor:
                    futures = {executor.submit(download_task, rf): rf for rf in to_download}

                    for future in as_completed(futures):
                        if not self._running or self._paused:
                            # Save progress before stopping
                            self._save_file_list_cache(remote_files, downloaded, listing_complete=True)
                            for f in futures:
                                f.cancel()
                            break

                        try:
                            path, success = future.result()
                            with completed_lock:
                                self._download_done += 1
                                if success:
                                    downloaded.add(path)
                                # Calculate speed for ETA
                                elapsed = time.time() - self._sync_start_time
                                if elapsed > 0:
                                    self._files_per_second = self._download_done / elapsed
                                if self._on_sync_progress:
                                    self._on_sync_progress(path, self._download_done, self._download_total)
                                # Log progress and save cache every 10 files
                                if self._download_done % 10 == 0:
                                    logger.info(f"Download progress: {self._download_done}/{self._download_total}")
                                    self._save_file_list_cache(remote_files, downloaded, listing_complete=True)
                        except Exception as e:
                            with completed_lock:
                                self._download_done += 1
                            logger.error(f"Download task error: {e}")

                self._is_downloading = False

            # Upload files in parallel
            if to_upload:
                self._upload_total = len(to_upload)
                self._upload_done = 0
                self._is_uploading = True
                self._sync_start_time = time.time()

                def upload_task(local_path: Path) -> Tuple[str, bool]:
                    if not self._running or self._paused:
                        return str(local_path), False
                    rel_path = local_path.relative_to(self.local_path)
                    success = self._upload_file(local_path, str(rel_path))
                    return str(rel_path), success

                with ThreadPoolExecutor(max_workers=self.PARALLEL_DOWNLOADS) as executor:
                    futures = {executor.submit(upload_task, lp): lp for lp in to_upload}

                    for future in as_completed(futures):
                        if not self._running or self._paused:
                            for f in futures:
                                f.cancel()
                            break

                        try:
                            path, success = future.result()
                            with completed_lock:
                                self._upload_done += 1
                                # Calculate speed for ETA
                                elapsed = time.time() - self._sync_start_time
                                if elapsed > 0:
                                    self._files_per_second = self._upload_done / elapsed
                                if self._on_sync_progress:
                                    self._on_sync_progress(path, self._upload_done, self._upload_total)
                                # Log progress every 10 files
                                if self._upload_done % 10 == 0:
                                    logger.info(f"Upload progress: {self._upload_done}/{self._upload_total}")
                        except Exception as e:
                            with completed_lock:
                                self._upload_done += 1
                            logger.error(f"Upload task error: {e}")

                self._is_uploading = False

            self._last_sync = time.time()

            # Clear cache if all files synced successfully
            if not to_download or len(downloaded) >= len(remote_files):
                self._clear_file_list_cache()
                logger.info("Sync completed, cache cleared")

            return True

        except Exception as e:
            logger.error(f"Sync error: {e}")
            return False

    def _is_remote_empty(self) -> bool:
        """Check if remote is empty."""
        try:
            result = subprocess.run(
                ['rclone', 'lsf', self.remote, '--max-depth', '1'],
                capture_output=True,
                text=True,
                timeout=30
            )
            return result.returncode == 0 and not result.stdout.strip()
        except subprocess.SubprocessError:
            return False

    def _scan_local_files(self) -> List[Path]:
        """Scan local folder for all files.

        Returns:
            List of file paths (excluding directories)
        """
        files = []
        for root, dirs, filenames in os.walk(self.local_path):
            # Skip hidden directories
            dirs[:] = [d for d in dirs if not d.startswith('.')]

            for name in filenames:
                # Skip hidden files
                if name.startswith('.'):
                    continue
                files.append(Path(root) / name)

        return files

    def _schedule_sync(self, debounce: float = None) -> None:
        """Schedule a sync with debouncing."""
        if debounce is None:
            debounce = self.SYNC_DEBOUNCE

        with self._lock:
            if not self._running or self._paused:
                return

            if self._sync_timer:
                self._sync_timer.cancel()

            self._sync_timer = threading.Timer(debounce, self._do_scheduled_sync)
            self._sync_timer.start()

    def _do_scheduled_sync(self) -> None:
        """Execute a scheduled sync."""
        with self._lock:
            self._sync_timer = None

        if self._running and not self._paused:
            self._run_sync()
            self._set_status(DaemonStatus.RUNNING)

    def _schedule_remote_check(self) -> None:
        """Schedule a periodic remote check."""
        with self._lock:
            if not self._running or self._paused:
                return

            if self._remote_check_timer:
                self._remote_check_timer.cancel()

            self._remote_check_timer = threading.Timer(
                self.REMOTE_CHECK_INTERVAL,
                self._do_remote_check
            )
            self._remote_check_timer.start()

    def _do_remote_check(self) -> None:
        """Check for remote changes and sync if needed."""
        with self._lock:
            self._remote_check_timer = None

        if not self._running or self._paused:
            return

        logger.debug("Checking for remote changes...")

        success = self._run_sync()

        if success:
            self._set_status(DaemonStatus.RUNNING)
        else:
            self._set_status(DaemonStatus.ERROR)

        self._schedule_remote_check()

    def _schedule_sync(self) -> None:
        """Schedule a sync after a short debounce period.

        This is called when local file changes are detected. Multiple rapid
        changes are coalesced into a single sync.
        """
        if self._paused or not self._running:
            return

        with self._lock:
            # Cancel existing timer
            if self._sync_timer:
                self._sync_timer.cancel()

            # Schedule sync after debounce period
            self._sync_timer = threading.Timer(
                self.SYNC_DEBOUNCE,
                self._run_scheduled_sync
            )
            self._sync_timer.start()
            logger.debug(f"Sync scheduled in {self.SYNC_DEBOUNCE}s")

    def _run_scheduled_sync(self) -> None:
        """Run a scheduled sync (called from timer)."""
        if self._paused or not self._running:
            return

        logger.info("Running scheduled sync (triggered by file changes)")
        self._run_sync()
        self._schedule_remote_check()

    # =========================================================================
    # Public API
    # =========================================================================

    def on_local_change(self, path: str, event_type: str) -> None:
        """Handle a local file change event.

        Called by the file watcher when a local file is modified.
        """
        logger.debug(f"Local change: {event_type} {path}")

        if event_type in ('create', 'modify'):
            self.db.mark_pending_upload(path)
        elif event_type == 'delete':
            self.db.delete_file(path)

        self._schedule_sync()

    def force_sync(self) -> Tuple[bool, str]:
        """Force an immediate sync."""
        if not self._running:
            return False, "Daemon not running"

        if self._paused:
            return False, "Sync is paused"

        with self._lock:
            if self._sync_timer:
                self._sync_timer.cancel()
                self._sync_timer = None

        success = self._run_sync()

        if success:
            self._set_status(DaemonStatus.RUNNING)
            return True, "Sync completed"
        else:
            self._set_status(DaemonStatus.ERROR)
            return False, "Sync failed"

    def force_resync(self) -> Tuple[bool, str]:
        """Force a full resync (clears local state and re-downloads)."""
        if not self._running:
            return False, "Daemon not running"

        if self._paused:
            return False, "Sync is paused"

        logger.warning("Forcing full resync - clearing local state...")

        # Clear the database
        conn = self.db._get_connection()
        conn.execute("DELETE FROM files")
        conn.commit()

        # Run sync
        success = self._run_sync()

        if success:
            self._set_status(DaemonStatus.RUNNING)
            return True, "Resync completed"
        else:
            self._set_status(DaemonStatus.ERROR)
            return False, "Resync failed"

    def get_file_status(self, path: str) -> Optional[SyncStatus]:
        """Get the sync status of a specific file."""
        return self.db.get_status(path)
