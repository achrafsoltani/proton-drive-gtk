"""SQLite database for tracking file sync state.

This module provides persistent storage for file sync states, allowing
the application to track which files are synced, pending, or in conflict.
"""

import sqlite3
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, List, Dict, Any


class SyncStatus(Enum):
    """Sync status for a file."""
    SYNCED = "synced"               # File matches remote
    PENDING_UPLOAD = "pending_upload"     # Local changes not yet uploaded
    PENDING_DOWNLOAD = "pending_download" # Remote changes not yet downloaded
    SYNCING = "syncing"             # Currently transferring
    CONFLICT = "conflict"           # Both sides changed
    ERROR = "error"                 # Sync failed


@dataclass
class FileState:
    """State information for a file."""
    path: str
    local_mtime: Optional[float] = None
    remote_mtime: Optional[float] = None
    local_size: Optional[int] = None
    remote_size: Optional[int] = None
    local_hash: Optional[str] = None
    remote_hash: Optional[str] = None
    status: SyncStatus = SyncStatus.SYNCED
    last_sync: Optional[float] = None
    error_message: Optional[str] = None


@dataclass
class SyncHistoryEntry:
    """Entry in the sync history log."""
    id: int
    timestamp: float
    action: str
    path: str
    result: str


class StateDatabase:
    """SQLite database for tracking file sync state.

    Thread-safe implementation using connection pooling.
    """

    def __init__(self, db_path: Optional[Path] = None):
        """Initialise the state database.

        Args:
            db_path: Path to the SQLite database file. Defaults to
                     ~/.cache/proton-drive-gtk/sync_state.db
        """
        if db_path is None:
            db_path = Path.home() / ".cache" / "proton-drive-gtk" / "sync_state.db"
        elif isinstance(db_path, str):
            db_path = Path(db_path)

        self.db_path = db_path
        self._local = threading.local()
        self._lock = threading.Lock()

        # Ensure directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Initialise schema
        self._init_schema()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a thread-local database connection."""
        if not hasattr(self._local, 'connection'):
            self._local.connection = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False
            )
            self._local.connection.row_factory = sqlite3.Row
        return self._local.connection

    def _init_schema(self) -> None:
        """Create database tables if they don't exist."""
        conn = self._get_connection()
        cursor = conn.cursor()

        # Files table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                local_mtime REAL,
                remote_mtime REAL,
                local_size INTEGER,
                remote_size INTEGER,
                local_hash TEXT,
                remote_hash TEXT,
                status TEXT DEFAULT 'synced',
                last_sync REAL,
                error_message TEXT
            )
        """)

        # Sync history table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sync_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                action TEXT NOT NULL,
                path TEXT NOT NULL,
                result TEXT
            )
        """)

        # Remote files table (cache for remote listing)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS remote_files (
                path TEXT PRIMARY KEY,
                name TEXT,
                size INTEGER,
                mod_time REAL,
                mime_type TEXT,
                file_id TEXT,
                downloaded INTEGER DEFAULT 0
            )
        """)

        # Sync session table (tracks listing completion)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sync_session (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                listing_complete INTEGER DEFAULT 0,
                listing_timestamp REAL,
                total_files INTEGER DEFAULT 0,
                downloaded_files INTEGER DEFAULT 0
            )
        """)

        # Indices for performance
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_files_status ON files(status)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_history_timestamp ON sync_history(timestamp)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_remote_downloaded ON remote_files(downloaded)
        """)

        conn.commit()

    def get_status(self, path: str) -> Optional[SyncStatus]:
        """Get the sync status for a file path.

        Args:
            path: Absolute path to the file

        Returns:
            SyncStatus enum value, or None if not tracked
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM files WHERE path = ?", (path,))
        row = cursor.fetchone()

        if row:
            try:
                return SyncStatus(row['status'])
            except ValueError:
                return SyncStatus.SYNCED
        return None

    def get_file_state(self, path: str) -> Optional[FileState]:
        """Get full state information for a file.

        Args:
            path: Absolute path to the file

        Returns:
            FileState object, or None if not tracked
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM files WHERE path = ?", (path,))
        row = cursor.fetchone()

        if row:
            return FileState(
                path=row['path'],
                local_mtime=row['local_mtime'],
                remote_mtime=row['remote_mtime'],
                local_size=row['local_size'],
                remote_size=row['remote_size'],
                local_hash=row['local_hash'],
                remote_hash=row['remote_hash'],
                status=SyncStatus(row['status']) if row['status'] else SyncStatus.SYNCED,
                last_sync=row['last_sync'],
                error_message=row['error_message']
            )
        return None

    def set_status(self, path: str, status: SyncStatus, error_message: Optional[str] = None) -> None:
        """Set the sync status for a file.

        Args:
            path: Absolute path to the file
            status: New sync status
            error_message: Optional error message (for ERROR status)
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO files (path, status, error_message, last_sync)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                status = excluded.status,
                error_message = excluded.error_message,
                last_sync = excluded.last_sync
        """, (path, status.value, error_message, time.time()))

        conn.commit()

    def update_file_state(self, state: FileState) -> None:
        """Update or insert full file state.

        Args:
            state: FileState object with all fields
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO files (
                path, local_mtime, remote_mtime, local_size, remote_size,
                local_hash, remote_hash, status, last_sync, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                local_mtime = excluded.local_mtime,
                remote_mtime = excluded.remote_mtime,
                local_size = excluded.local_size,
                remote_size = excluded.remote_size,
                local_hash = excluded.local_hash,
                remote_hash = excluded.remote_hash,
                status = excluded.status,
                last_sync = excluded.last_sync,
                error_message = excluded.error_message
        """, (
            state.path,
            state.local_mtime,
            state.remote_mtime,
            state.local_size,
            state.remote_size,
            state.local_hash,
            state.remote_hash,
            state.status.value,
            state.last_sync,
            state.error_message
        ))

        conn.commit()

    def mark_synced(self, path: str, local_mtime: float, local_size: int,
                    local_hash: Optional[str] = None) -> None:
        """Mark a file as synced with current local state.

        Args:
            path: Absolute path to the file
            local_mtime: Local modification time
            local_size: Local file size in bytes
            local_hash: Optional file hash
        """
        now = time.time()
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO files (path, local_mtime, remote_mtime, local_size,
                              remote_size, local_hash, remote_hash, status, last_sync)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'synced', ?)
            ON CONFLICT(path) DO UPDATE SET
                local_mtime = excluded.local_mtime,
                remote_mtime = excluded.remote_mtime,
                local_size = excluded.local_size,
                remote_size = excluded.remote_size,
                local_hash = excluded.local_hash,
                remote_hash = excluded.remote_hash,
                status = 'synced',
                last_sync = excluded.last_sync,
                error_message = NULL
        """, (path, local_mtime, local_mtime, local_size, local_size,
              local_hash, local_hash, now))

        conn.commit()

    def mark_pending_upload(self, path: str) -> None:
        """Mark a file as having local changes pending upload.

        Args:
            path: Absolute path to the file
        """
        self.set_status(path, SyncStatus.PENDING_UPLOAD)

    def mark_pending_download(self, path: str) -> None:
        """Mark a file as having remote changes pending download.

        Args:
            path: Absolute path to the file
        """
        self.set_status(path, SyncStatus.PENDING_DOWNLOAD)

    def mark_syncing(self, path: str) -> None:
        """Mark a file as currently syncing.

        Args:
            path: Absolute path to the file
        """
        self.set_status(path, SyncStatus.SYNCING)

    def mark_conflict(self, path: str) -> None:
        """Mark a file as having a sync conflict.

        Args:
            path: Absolute path to the file
        """
        self.set_status(path, SyncStatus.CONFLICT)

    def mark_error(self, path: str, error_message: str) -> None:
        """Mark a file as having a sync error.

        Args:
            path: Absolute path to the file
            error_message: Description of the error
        """
        self.set_status(path, SyncStatus.ERROR, error_message)

    def delete_file(self, path: str) -> None:
        """Remove a file from the state database.

        Args:
            path: Absolute path to the file
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM files WHERE path = ?", (path,))
        conn.commit()

    def get_files_by_status(self, status: SyncStatus) -> List[str]:
        """Get all files with a specific status.

        Args:
            status: The sync status to filter by

        Returns:
            List of file paths
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT path FROM files WHERE status = ?", (status.value,))
        return [row['path'] for row in cursor.fetchall()]

    def get_pending_files(self) -> List[str]:
        """Get all files pending sync (upload or download).

        Returns:
            List of file paths
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT path FROM files
            WHERE status IN ('pending_upload', 'pending_download', 'syncing')
        """)
        return [row['path'] for row in cursor.fetchall()]

    def get_stats(self) -> Dict[str, int]:
        """Get statistics about sync state.

        Returns:
            Dictionary with counts for each status
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT status, COUNT(*) as count
            FROM files
            GROUP BY status
        """)

        stats = {status.value: 0 for status in SyncStatus}
        for row in cursor.fetchall():
            stats[row['status']] = row['count']

        return stats

    def log_sync_action(self, action: str, path: str, result: str) -> None:
        """Log a sync action to history.

        Args:
            action: Type of action (upload, download, delete, conflict)
            path: File path involved
            result: Result of the action (success, failed, skipped)
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO sync_history (timestamp, action, path, result)
            VALUES (?, ?, ?, ?)
        """, (time.time(), action, path, result))
        conn.commit()

    def get_recent_history(self, limit: int = 100) -> List[SyncHistoryEntry]:
        """Get recent sync history entries.

        Args:
            limit: Maximum number of entries to return

        Returns:
            List of SyncHistoryEntry objects, most recent first
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM sync_history
            ORDER BY timestamp DESC
            LIMIT ?
        """, (limit,))

        return [
            SyncHistoryEntry(
                id=row['id'],
                timestamp=row['timestamp'],
                action=row['action'],
                path=row['path'],
                result=row['result']
            )
            for row in cursor.fetchall()
        ]

    def clear_history(self, before_timestamp: Optional[float] = None) -> int:
        """Clear sync history.

        Args:
            before_timestamp: Only clear entries before this timestamp.
                            If None, clears all history.

        Returns:
            Number of entries deleted
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        if before_timestamp:
            cursor.execute(
                "DELETE FROM sync_history WHERE timestamp < ?",
                (before_timestamp,)
            )
        else:
            cursor.execute("DELETE FROM sync_history")

        deleted = cursor.rowcount
        conn.commit()
        return deleted

    def vacuum(self) -> None:
        """Compact the database file."""
        conn = self._get_connection()
        conn.execute("VACUUM")

    def close(self) -> None:
        """Close all database connections."""
        if hasattr(self._local, 'connection'):
            self._local.connection.close()
            del self._local.connection

    # =========================================================================
    # Remote File Cache (replaces JSON cache file)
    # =========================================================================

    def save_remote_files(self, files: List[Dict[str, Any]], mark_listing_complete: bool = False) -> None:
        """Save remote file list to database.

        Args:
            files: List of dicts with path, name, size, mod_time, mime_type, file_id
            mark_listing_complete: Whether to mark listing as complete
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # Insert/update remote files
        cursor.executemany("""
            INSERT INTO remote_files (path, name, size, mod_time, mime_type, file_id, downloaded)
            VALUES (:path, :name, :size, :mod_time, :mime_type, :file_id, 0)
            ON CONFLICT(path) DO UPDATE SET
                name = excluded.name,
                size = excluded.size,
                mod_time = excluded.mod_time,
                mime_type = excluded.mime_type,
                file_id = excluded.file_id
        """, files)

        # Update session
        cursor.execute("""
            INSERT INTO sync_session (id, listing_complete, listing_timestamp, total_files)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                listing_complete = excluded.listing_complete,
                listing_timestamp = excluded.listing_timestamp,
                total_files = excluded.total_files
        """, (1 if mark_listing_complete else 0, time.time(), len(files)))

        conn.commit()

    def get_remote_files(self) -> Optional[List[Dict[str, Any]]]:
        """Get cached remote file list if listing is complete.

        Returns:
            List of file dicts, or None if no complete listing cached
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # Check if we have a complete listing
        cursor.execute("SELECT listing_complete, listing_timestamp FROM sync_session WHERE id = 1")
        row = cursor.fetchone()

        if not row or not row['listing_complete']:
            return None

        # Check if cache is too old (24 hours)
        if time.time() - row['listing_timestamp'] > 86400:
            return None

        # Get all remote files
        cursor.execute("SELECT path, name, size, mod_time, mime_type, file_id, downloaded FROM remote_files")
        return [dict(row) for row in cursor.fetchall()]

    def get_pending_remote_files(self) -> List[Dict[str, Any]]:
        """Get remote files that haven't been downloaded yet.

        Returns:
            List of file dicts where downloaded = 0
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT path, name, size, mod_time, mime_type, file_id FROM remote_files WHERE downloaded = 0")
        return [dict(row) for row in cursor.fetchall()]

    def mark_remote_file_downloaded(self, path: str) -> None:
        """Mark a remote file as downloaded.

        Args:
            path: Remote file path
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE remote_files SET downloaded = 1 WHERE path = ?", (path,))

        # Update downloaded count in session
        cursor.execute("""
            UPDATE sync_session SET downloaded_files = (
                SELECT COUNT(*) FROM remote_files WHERE downloaded = 1
            ) WHERE id = 1
        """)
        conn.commit()

    def get_remote_files_progress(self) -> Dict[str, int]:
        """Get remote files download progress.

        Returns:
            Dict with total_files, downloaded_files, pending_files
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) as total FROM remote_files")
        total = cursor.fetchone()['total']

        cursor.execute("SELECT COUNT(*) as downloaded FROM remote_files WHERE downloaded = 1")
        downloaded = cursor.fetchone()['downloaded']

        return {
            'total_files': total,
            'downloaded_files': downloaded,
            'pending_files': total - downloaded
        }

    def is_listing_complete(self) -> bool:
        """Check if remote listing is complete and cached.

        Returns:
            True if a complete listing exists
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT listing_complete FROM sync_session WHERE id = 1")
        row = cursor.fetchone()
        return row is not None and row['listing_complete'] == 1

    def clear_remote_files_cache(self) -> None:
        """Clear the remote files cache."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM remote_files")
        cursor.execute("DELETE FROM sync_session")
        conn.commit()

    def seed_cache_from_history(self, local_path: str) -> int:
        """Seed the remote files cache from successful download history.

        This allows recovering the file list from sync_history when the
        remote_files cache is empty but downloads have been completed.
        Uses local file metadata since the files are already downloaded.

        Args:
            local_path: The local sync directory path (to strip from paths)

        Returns:
            Number of files seeded into cache
        """
        import os
        from pathlib import Path

        conn = self._get_connection()
        cursor = conn.cursor()

        # Check if cache already has files
        cursor.execute("SELECT COUNT(*) as cnt FROM remote_files")
        if cursor.fetchone()['cnt'] > 0:
            return 0  # Cache already populated

        # Get all successful downloads from history
        cursor.execute("""
            SELECT DISTINCT path FROM sync_history
            WHERE action = 'download' AND result = 'success'
        """)
        rows = cursor.fetchall()

        if not rows:
            return 0

        # Convert absolute paths to relative paths and get local file metadata
        local_prefix = local_path.rstrip('/') + '/'
        seeded = 0

        for row in rows:
            abs_path = row['path']
            if abs_path.startswith(local_prefix):
                rel_path = abs_path[len(local_prefix):]
                local_file = Path(abs_path)

                # Get file metadata from local file
                size = 0
                mod_time = 0.0
                name = local_file.name
                if local_file.exists():
                    try:
                        stat = local_file.stat()
                        size = stat.st_size
                        mod_time = stat.st_mtime
                    except OSError:
                        pass

                cursor.execute("""
                    INSERT OR IGNORE INTO remote_files (path, name, size, mod_time, downloaded)
                    VALUES (?, ?, ?, ?, 1)
                """, (rel_path, name, size, mod_time))
                seeded += 1

        if seeded > 0:
            # Mark listing as complete since we have downloaded files
            cursor.execute("""
                INSERT OR REPLACE INTO sync_session (id, listing_complete, listing_timestamp, total_files, downloaded_files)
                VALUES (1, 1, ?, ?, ?)
            """, (time.time(), seeded, seeded))
            conn.commit()

        return seeded
