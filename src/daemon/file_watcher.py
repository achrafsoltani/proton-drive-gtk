"""File watcher using inotify for local change detection.

This module monitors the local sync folder for changes and notifies
the sync daemon when files are created, modified, or deleted.
"""

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Set, Dict
from collections import defaultdict

logger = logging.getLogger(__name__)

# Event types
EVENT_CREATE = 'create'
EVENT_MODIFY = 'modify'
EVENT_DELETE = 'delete'
EVENT_MOVE = 'move'

# Try to import inotify - fall back to polling if not available
try:
    import inotify.adapters
    import inotify.constants
    INOTIFY_AVAILABLE = True
except ImportError:
    INOTIFY_AVAILABLE = False
    logger.warning("inotify not available, will use polling fallback")


class FileWatcher:
    """Watch local folder for changes using inotify.

    Uses Linux inotify for efficient real-time file change detection.
    Falls back to polling if inotify is not available.
    """

    # Debounce time in seconds - coalesce rapid changes
    DEBOUNCE_TIME = 1.0

    def __init__(self, path: str, callback: Callable[[str, str], None]):
        """Initialise the file watcher.

        Args:
            path: Path to the folder to watch
            callback: Function to call when a file changes.
                     Receives (file_path, event_type) arguments.
        """
        self.path = Path(path).expanduser().resolve()
        self.callback = callback
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._ignore_patterns: Set[str] = {
            '.git',
            '.DS_Store',
            '*.tmp',
            '*.swp',
            '*.swx',
            '*~',
            '.~*',
            '*.part',
            '*.crdownload',
            '.rclone*',
        }
        # Debounce: track pending events
        self._pending_events: Dict[str, tuple] = {}
        self._pending_lock = threading.Lock()
        self._debounce_timer: Optional[threading.Timer] = None

    def start(self) -> bool:
        """Start watching for file changes.

        Returns:
            True if watcher started successfully
        """
        if self._running:
            return True

        if not self.path.exists():
            logger.error(f"Watch path does not exist: {self.path}")
            return False

        self._running = True

        if INOTIFY_AVAILABLE:
            self._thread = threading.Thread(target=self._watch_inotify, daemon=True)
            self._thread.start()
            logger.info(f"File watcher started (inotify) for: {self.path}")
        else:
            self._thread = threading.Thread(target=self._watch_polling, daemon=True)
            self._thread.start()
            logger.info(f"File watcher started (polling) for: {self.path}")

        return True

    def stop(self) -> None:
        """Stop watching for file changes."""
        self._running = False

        if self._debounce_timer:
            self._debounce_timer.cancel()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

        logger.info("File watcher stopped")

    def is_running(self) -> bool:
        """Check if the watcher is running."""
        return self._running

    def add_ignore_pattern(self, pattern: str) -> None:
        """Add a pattern to ignore."""
        self._ignore_patterns.add(pattern)

    def remove_ignore_pattern(self, pattern: str) -> None:
        """Remove an ignore pattern."""
        self._ignore_patterns.discard(pattern)

    def _should_ignore(self, path: str) -> bool:
        """Check if a path should be ignored."""
        import fnmatch

        name = os.path.basename(path)
        for pattern in self._ignore_patterns:
            if fnmatch.fnmatch(name, pattern):
                return True
            # Also check directory components
            if pattern in path:
                return True

        return False

    def _queue_event(self, filepath: str, event_type: str):
        """Queue an event for debounced processing."""
        if self._should_ignore(filepath):
            return

        with self._pending_lock:
            # Store the latest event for this file
            self._pending_events[filepath] = (event_type, time.time())

            # Reset debounce timer
            if self._debounce_timer:
                self._debounce_timer.cancel()

            self._debounce_timer = threading.Timer(
                self.DEBOUNCE_TIME, self._flush_events
            )
            self._debounce_timer.start()

    def _flush_events(self):
        """Flush pending events to callback."""
        with self._pending_lock:
            events = self._pending_events.copy()
            self._pending_events.clear()

        for filepath, (event_type, _) in events.items():
            try:
                logger.debug(f"File event: {event_type} {filepath}")
                self.callback(filepath, event_type)
            except Exception as e:
                logger.error(f"Error in file watcher callback: {e}")

    def _watch_inotify(self):
        """Watch using inotify (Linux)."""
        try:
            i = inotify.adapters.InotifyTree(
                str(self.path),
                mask=(
                    inotify.constants.IN_CREATE |
                    inotify.constants.IN_MODIFY |
                    inotify.constants.IN_DELETE |
                    inotify.constants.IN_MOVED_FROM |
                    inotify.constants.IN_MOVED_TO |
                    inotify.constants.IN_CLOSE_WRITE
                )
            )

            logger.info(f"Inotify watching: {self.path}")

            for event in i.event_gen(yield_nones=True):
                if not self._running:
                    break

                if event is None:
                    continue

                (_, type_names, path, filename) = event

                if not filename:
                    continue

                filepath = os.path.join(path, filename)

                # Map inotify events to our event types
                if 'IN_CREATE' in type_names:
                    self._queue_event(filepath, EVENT_CREATE)
                elif 'IN_CLOSE_WRITE' in type_names:
                    # File was written and closed - most reliable for modifications
                    self._queue_event(filepath, EVENT_MODIFY)
                elif 'IN_DELETE' in type_names:
                    self._queue_event(filepath, EVENT_DELETE)
                elif 'IN_MOVED_FROM' in type_names:
                    self._queue_event(filepath, EVENT_DELETE)
                elif 'IN_MOVED_TO' in type_names:
                    self._queue_event(filepath, EVENT_CREATE)

        except Exception as e:
            logger.error(f"Inotify watcher error: {e}")
            if self._running:
                # Fall back to polling
                logger.info("Falling back to polling mode")
                self._watch_polling()

    def _watch_polling(self):
        """Watch using polling (fallback)."""
        POLL_INTERVAL = 5  # seconds

        # Initial scan
        known_files: Dict[str, float] = {}
        for root, dirs, files in os.walk(self.path):
            # Skip ignored directories
            dirs[:] = [d for d in dirs if not self._should_ignore(d)]

            for f in files:
                filepath = os.path.join(root, f)
                if not self._should_ignore(filepath):
                    try:
                        known_files[filepath] = os.path.getmtime(filepath)
                    except OSError:
                        pass

        logger.info(f"Polling watcher started with {len(known_files)} files")

        while self._running:
            time.sleep(POLL_INTERVAL)

            if not self._running:
                break

            current_files: Dict[str, float] = {}
            for root, dirs, files in os.walk(self.path):
                dirs[:] = [d for d in dirs if not self._should_ignore(d)]

                for f in files:
                    filepath = os.path.join(root, f)
                    if not self._should_ignore(filepath):
                        try:
                            current_files[filepath] = os.path.getmtime(filepath)
                        except OSError:
                            pass

            # Check for new and modified files
            for filepath, mtime in current_files.items():
                if filepath not in known_files:
                    self._queue_event(filepath, EVENT_CREATE)
                elif mtime > known_files[filepath]:
                    self._queue_event(filepath, EVENT_MODIFY)

            # Check for deleted files
            for filepath in known_files:
                if filepath not in current_files:
                    self._queue_event(filepath, EVENT_DELETE)

            known_files = current_files
