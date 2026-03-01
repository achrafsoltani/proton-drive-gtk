// Package db provides SQLite-based state management for the sync daemon.
package db

import (
	"database/sql"
	"sync"
	"time"

	_ "modernc.org/sqlite"
)

// SyncStatus represents the sync status of a file.
type SyncStatus string

const (
	StatusSynced          SyncStatus = "synced"
	StatusPendingUpload   SyncStatus = "pending_upload"
	StatusPendingDownload SyncStatus = "pending_download"
	StatusSyncing         SyncStatus = "syncing"
	StatusConflict        SyncStatus = "conflict"
	StatusError           SyncStatus = "error"
)

// FileState represents the sync state of a file.
type FileState struct {
	Path         string
	LocalMTime   float64
	RemoteMTime  float64
	LocalSize    int64
	RemoteSize   int64
	Status       SyncStatus
	LastSync     float64
	ErrorMessage string
}

// RemoteFile represents a file on the remote.
type RemoteFile struct {
	Path       string
	Name       string
	Size       int64
	ModTime    float64
	MimeType   string
	FileID     string
	Downloaded bool
}

// SyncHistoryEntry represents a sync history entry.
type SyncHistoryEntry struct {
	ID        int64
	Timestamp float64
	Action    string
	Path      string
	Result    string
}

// StateDB provides thread-safe access to the sync state database.
type StateDB struct {
	db      *sql.DB
	writeMu sync.Mutex // Serialise all writes
}

// Open opens the state database.
func Open(dbPath string) (*StateDB, error) {
	db, err := sql.Open("sqlite", dbPath+"?_journal_mode=WAL&_busy_timeout=30000")
	if err != nil {
		return nil, err
	}

	// Single connection prevents lock contention
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)

	sdb := &StateDB{db: db}
	if err := sdb.initSchema(); err != nil {
		db.Close()
		return nil, err
	}

	return sdb, nil
}

// Close closes the database.
func (s *StateDB) Close() error {
	return s.db.Close()
}

func (s *StateDB) initSchema() error {
	s.writeMu.Lock()
	defer s.writeMu.Unlock()

	schema := `
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
		);
		CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);

		CREATE TABLE IF NOT EXISTS remote_files (
			path TEXT PRIMARY KEY,
			name TEXT,
			size INTEGER,
			mod_time REAL,
			mime_type TEXT,
			file_id TEXT,
			downloaded INTEGER DEFAULT 0
		);
		CREATE INDEX IF NOT EXISTS idx_remote_downloaded ON remote_files(downloaded);

		CREATE TABLE IF NOT EXISTS sync_history (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			timestamp REAL NOT NULL,
			action TEXT NOT NULL,
			path TEXT NOT NULL,
			result TEXT
		);
		CREATE INDEX IF NOT EXISTS idx_history_timestamp ON sync_history(timestamp);

		CREATE TABLE IF NOT EXISTS sync_session (
			id INTEGER PRIMARY KEY,
			listing_complete INTEGER DEFAULT 0,
			listing_timestamp REAL,
			total_files INTEGER DEFAULT 0,
			downloaded_files INTEGER DEFAULT 0
		);
	`
	_, err := s.db.Exec(schema)
	return err
}

// GetFileState returns the state of a file.
func (s *StateDB) GetFileState(path string) (*FileState, error) {
	row := s.db.QueryRow(`
		SELECT path, local_mtime, remote_mtime, local_size, remote_size,
		       status, last_sync, error_message
		FROM files WHERE path = ?
	`, path)

	var state FileState
	var errMsg sql.NullString
	err := row.Scan(
		&state.Path, &state.LocalMTime, &state.RemoteMTime,
		&state.LocalSize, &state.RemoteSize, &state.Status,
		&state.LastSync, &errMsg,
	)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	state.ErrorMessage = errMsg.String
	return &state, nil
}

// SetFileState sets the state of a file.
func (s *StateDB) SetFileState(state *FileState) error {
	s.writeMu.Lock()
	defer s.writeMu.Unlock()

	_, err := s.db.Exec(`
		INSERT INTO files (path, local_mtime, remote_mtime, local_size, remote_size, status, last_sync, error_message)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(path) DO UPDATE SET
			local_mtime = excluded.local_mtime,
			remote_mtime = excluded.remote_mtime,
			local_size = excluded.local_size,
			remote_size = excluded.remote_size,
			status = excluded.status,
			last_sync = excluded.last_sync,
			error_message = excluded.error_message
	`, state.Path, state.LocalMTime, state.RemoteMTime, state.LocalSize, state.RemoteSize,
		state.Status, state.LastSync, state.ErrorMessage)
	return err
}

// MarkSynced marks a file as synced.
func (s *StateDB) MarkSynced(path string, mtime float64, size int64) error {
	s.writeMu.Lock()
	defer s.writeMu.Unlock()

	now := float64(time.Now().Unix())
	_, err := s.db.Exec(`
		INSERT INTO files (path, local_mtime, remote_mtime, local_size, remote_size, status, last_sync)
		VALUES (?, ?, ?, ?, ?, 'synced', ?)
		ON CONFLICT(path) DO UPDATE SET
			local_mtime = excluded.local_mtime,
			remote_mtime = excluded.remote_mtime,
			local_size = excluded.local_size,
			remote_size = excluded.remote_size,
			status = 'synced',
			last_sync = excluded.last_sync,
			error_message = NULL
	`, path, mtime, mtime, size, size, now)
	return err
}

// SaveRemoteFile saves a remote file to the cache.
func (s *StateDB) SaveRemoteFile(rf *RemoteFile) error {
	s.writeMu.Lock()
	defer s.writeMu.Unlock()

	downloaded := 0
	if rf.Downloaded {
		downloaded = 1
	}

	_, err := s.db.Exec(`
		INSERT INTO remote_files (path, name, size, mod_time, mime_type, file_id, downloaded)
		VALUES (?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(path) DO UPDATE SET
			name = excluded.name,
			size = excluded.size,
			mod_time = excluded.mod_time,
			mime_type = excluded.mime_type,
			file_id = excluded.file_id
	`, rf.Path, rf.Name, rf.Size, rf.ModTime, rf.MimeType, rf.FileID, downloaded)
	return err
}

// SaveRemoteFilesBatch saves multiple remote files in a single transaction.
func (s *StateDB) SaveRemoteFilesBatch(files []*RemoteFile) error {
	s.writeMu.Lock()
	defer s.writeMu.Unlock()

	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()

	stmt, err := tx.Prepare(`
		INSERT INTO remote_files (path, name, size, mod_time, mime_type, file_id, downloaded)
		VALUES (?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(path) DO UPDATE SET
			name = excluded.name,
			size = excluded.size,
			mod_time = excluded.mod_time,
			mime_type = excluded.mime_type,
			file_id = excluded.file_id
	`)
	if err != nil {
		return err
	}
	defer stmt.Close()

	for _, rf := range files {
		downloaded := 0
		if rf.Downloaded {
			downloaded = 1
		}
		if _, err := stmt.Exec(rf.Path, rf.Name, rf.Size, rf.ModTime, rf.MimeType, rf.FileID, downloaded); err != nil {
			return err
		}
	}

	return tx.Commit()
}

// MarkRemoteFileDownloaded marks a remote file as downloaded.
func (s *StateDB) MarkRemoteFileDownloaded(path string) error {
	s.writeMu.Lock()
	defer s.writeMu.Unlock()

	_, err := s.db.Exec(`UPDATE remote_files SET downloaded = 1 WHERE path = ?`, path)
	return err
}

// GetPendingRemoteFiles returns files not yet downloaded.
func (s *StateDB) GetPendingRemoteFiles() ([]*RemoteFile, error) {
	rows, err := s.db.Query(`
		SELECT path, name, size, mod_time, mime_type, file_id
		FROM remote_files WHERE downloaded = 0
	`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var files []*RemoteFile
	for rows.Next() {
		rf := &RemoteFile{}
		if err := rows.Scan(&rf.Path, &rf.Name, &rf.Size, &rf.ModTime, &rf.MimeType, &rf.FileID); err != nil {
			return nil, err
		}
		files = append(files, rf)
	}
	return files, rows.Err()
}

// IsListingComplete returns whether the remote listing is complete.
func (s *StateDB) IsListingComplete() bool {
	var complete int
	err := s.db.QueryRow(`SELECT listing_complete FROM sync_session WHERE id = 1`).Scan(&complete)
	if err != nil {
		return false
	}
	return complete == 1
}

// SetListingComplete marks the listing as complete.
func (s *StateDB) SetListingComplete(totalFiles int) error {
	s.writeMu.Lock()
	defer s.writeMu.Unlock()

	_, err := s.db.Exec(`
		INSERT INTO sync_session (id, listing_complete, listing_timestamp, total_files)
		VALUES (1, 1, ?, ?)
		ON CONFLICT(id) DO UPDATE SET
			listing_complete = 1,
			listing_timestamp = excluded.listing_timestamp,
			total_files = excluded.total_files
	`, float64(time.Now().Unix()), totalFiles)
	return err
}

// ClearRemoteFilesCache clears the remote files cache.
func (s *StateDB) ClearRemoteFilesCache() error {
	s.writeMu.Lock()
	defer s.writeMu.Unlock()

	_, err := s.db.Exec(`DELETE FROM remote_files; DELETE FROM sync_session;`)
	return err
}

// AddSyncHistory adds a sync history entry.
func (s *StateDB) AddSyncHistory(action, path, result string) error {
	s.writeMu.Lock()
	defer s.writeMu.Unlock()

	_, err := s.db.Exec(`
		INSERT INTO sync_history (timestamp, action, path, result)
		VALUES (?, ?, ?, ?)
	`, float64(time.Now().Unix()), action, path, result)
	return err
}

// GetRecentHistory returns recent sync history entries.
func (s *StateDB) GetRecentHistory(limit int) ([]*SyncHistoryEntry, error) {
	rows, err := s.db.Query(`
		SELECT id, timestamp, action, path, result
		FROM sync_history ORDER BY timestamp DESC LIMIT ?
	`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var entries []*SyncHistoryEntry
	for rows.Next() {
		e := &SyncHistoryEntry{}
		var result sql.NullString
		if err := rows.Scan(&e.ID, &e.Timestamp, &e.Action, &e.Path, &result); err != nil {
			return nil, err
		}
		e.Result = result.String
		entries = append(entries, e)
	}
	return entries, rows.Err()
}

// GetStats returns sync statistics in a single query.
func (s *StateDB) GetStats() (totalFiles, syncedFiles, pendingUpload, pendingDownload, errors int) {
	s.db.QueryRow(`
		SELECT
			(SELECT COUNT(*) FROM remote_files),
			(SELECT COUNT(*) FROM remote_files WHERE downloaded = 1),
			(SELECT COUNT(*) FROM files WHERE status = 'pending_upload'),
			(SELECT COUNT(*) FROM remote_files WHERE downloaded = 0),
			(SELECT COUNT(*) FROM files WHERE status = 'error')
	`).Scan(&totalFiles, &syncedFiles, &pendingUpload, &pendingDownload, &errors)
	return
}
