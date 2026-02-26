// Package sync provides the core sync engine.
package sync

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"time"

	"github.com/achrafsoltani/proton-drive-gtk/go-daemon/internal/db"
	"github.com/achrafsoltani/proton-drive-gtk/go-daemon/internal/rclone"
	"github.com/achrafsoltani/proton-drive-gtk/go-daemon/internal/watcher"
)

// Status represents the daemon status.
type Status string

const (
	StatusStopped  Status = "stopped"
	StatusStarting Status = "starting"
	StatusRunning  Status = "running"
	StatusSyncing  Status = "syncing"
	StatusPaused   Status = "paused"
	StatusError    Status = "error"
)

// Stats holds sync statistics.
type Stats struct {
	Status          Status
	TotalFiles      int
	SyncedFiles     int
	PendingUpload   int
	PendingDownload int
	Errors          int
	CurrentFile     string
	IsListing       bool
	IsDownloading   bool
	IsUploading     bool
	DownloadTotal   int
	DownloadDone    int
	UploadTotal     int
	UploadDone      int
	ETASeconds      int
}

// Engine is the core sync engine.
type Engine struct {
	localPath   string
	remoteName  string
	db          *db.StateDB
	rclone      *rclone.Client
	watcher     *watcher.Watcher
	logger      *slog.Logger

	mu          sync.RWMutex
	status      Status
	currentFile string
	paused      bool

	// Progress tracking
	isListing       atomic.Bool
	isDownloading   atomic.Bool
	isUploading     atomic.Bool
	downloadTotal   atomic.Int32
	downloadDone    atomic.Int32
	uploadTotal     atomic.Int32
	uploadDone      atomic.Int32
	listingFiles    atomic.Int32
	listingDirs     atomic.Int32

	// Sync scheduling
	syncInterval time.Duration
	syncTimer    *time.Timer
	syncMu       sync.Mutex

	// Concurrency control
	syncRunning   atomic.Bool        // Prevents concurrent Sync() calls
	downloadSem   chan struct{}      // Limits concurrent downloads
	activeDownloads sync.Map         // Tracks files being downloaded (to ignore watcher events)
}

// NewEngine creates a new sync engine.
func NewEngine(localPath, remoteName string, stateDB *db.StateDB, w *watcher.Watcher, logger *slog.Logger, maxTransfers int) *Engine {
	if maxTransfers < 1 {
		maxTransfers = 4
	}
	return &Engine{
		localPath:    localPath,
		remoteName:   remoteName,
		db:           stateDB,
		rclone:       rclone.NewClient(remoteName, logger),
		watcher:      w,
		logger:       logger,
		status:       StatusStopped,
		syncInterval: 60 * time.Second,
		downloadSem:  make(chan struct{}, maxTransfers),
	}
}

// Start starts the sync engine.
func (e *Engine) Start(ctx context.Context) error {
	e.mu.Lock()
	e.status = StatusStarting
	e.mu.Unlock()

	// Check rclone
	if err := e.rclone.CheckRemote(ctx); err != nil {
		e.setStatus(StatusError)
		return err
	}

	// Start file watcher
	if e.watcher != nil {
		if err := e.watcher.Start(ctx); err != nil {
			e.logger.Warn("failed to start file watcher", "error", err)
		} else {
			go e.handleFileEvents(ctx)
		}
	}

	e.setStatus(StatusRunning)

	// Initial sync
	go func() {
		if err := e.Sync(ctx); err != nil {
			e.logger.Error("initial sync failed", "error", err)
		}
		e.scheduleNextSync(ctx)
	}()

	return nil
}

// Stop stops the sync engine.
func (e *Engine) Stop() {
	e.syncMu.Lock()
	if e.syncTimer != nil {
		e.syncTimer.Stop()
	}
	e.syncMu.Unlock()

	if e.watcher != nil {
		e.watcher.Stop()
	}

	e.setStatus(StatusStopped)
}

// Pause pauses syncing.
func (e *Engine) Pause() {
	e.mu.Lock()
	e.paused = true
	e.status = StatusPaused
	e.mu.Unlock()
	e.logger.Info("sync paused")
}

// Resume resumes syncing.
func (e *Engine) Resume() {
	e.mu.Lock()
	e.paused = false
	e.status = StatusRunning
	e.mu.Unlock()
	e.logger.Info("sync resumed")
}

// IsPaused returns whether syncing is paused.
func (e *Engine) IsPaused() bool {
	e.mu.RLock()
	defer e.mu.RUnlock()
	return e.paused
}

// Sync performs a full sync cycle.
func (e *Engine) Sync(ctx context.Context) error {
	if e.IsPaused() {
		return nil
	}

	// Prevent concurrent sync runs
	if !e.syncRunning.CompareAndSwap(false, true) {
		e.logger.Debug("sync already running, skipping")
		return nil
	}
	defer e.syncRunning.Store(false)

	e.setStatus(StatusSyncing)
	defer e.setStatus(StatusRunning)

	e.logger.Info("starting sync")

	// List remote files
	if err := e.listRemoteFiles(ctx); err != nil {
		e.logger.Error("failed to list remote files", "error", err)
		return err
	}

	// Scan local files
	localFiles, err := e.scanLocalFiles()
	if err != nil {
		e.logger.Error("failed to scan local files", "error", err)
		return err
	}
	e.logger.Info("scanned local files", "count", len(localFiles))

	// Get pending downloads
	pending, err := e.db.GetPendingRemoteFiles()
	if err != nil {
		return err
	}

	// Download missing files
	if len(pending) > 0 {
		if err := e.downloadFiles(ctx, pending); err != nil {
			e.logger.Error("download failed", "error", err)
		}
	}

	// Upload local-only files
	if err := e.uploadNewFiles(ctx, localFiles); err != nil {
		e.logger.Error("upload failed", "error", err)
	}

	e.logger.Info("sync completed")
	return nil
}

// ForceSync forces an immediate sync.
func (e *Engine) ForceSync(ctx context.Context) error {
	return e.Sync(ctx)
}

// ClearCache clears the remote file cache.
func (e *Engine) ClearCache() error {
	return e.db.ClearRemoteFilesCache()
}

// GetStats returns current sync statistics.
func (e *Engine) GetStats() Stats {
	e.mu.RLock()
	status := e.status
	currentFile := e.currentFile
	e.mu.RUnlock()

	total, synced, pendingUp, pendingDown, errors := e.db.GetStats()

	return Stats{
		Status:          status,
		TotalFiles:      total,
		SyncedFiles:     synced,
		PendingUpload:   pendingUp,
		PendingDownload: pendingDown,
		Errors:          errors,
		CurrentFile:     currentFile,
		IsListing:       e.isListing.Load(),
		IsDownloading:   e.isDownloading.Load(),
		IsUploading:     e.isUploading.Load(),
		DownloadTotal:   int(e.downloadTotal.Load()),
		DownloadDone:    int(e.downloadDone.Load()),
		UploadTotal:     int(e.uploadTotal.Load()),
		UploadDone:      int(e.uploadDone.Load()),
	}
}

// GetStatus returns the file status for a path.
func (e *Engine) GetStatus(path string) (string, error) {
	// Get relative path
	relPath, err := filepath.Rel(e.localPath, path)
	if err != nil {
		return "unknown", nil
	}

	// Check if file exists locally
	_, err = os.Stat(path)
	localExists := err == nil

	// Check database state
	state, err := e.db.GetFileState(relPath)
	if err != nil {
		return "unknown", err
	}

	if state != nil {
		return string(state.Status), nil
	}

	if localExists {
		return "synced", nil
	}

	return "cloud", nil
}

func (e *Engine) setStatus(status Status) {
	e.mu.Lock()
	e.status = status
	e.mu.Unlock()
}

func (e *Engine) setCurrentFile(path string) {
	e.mu.Lock()
	e.currentFile = path
	e.mu.Unlock()
}

func (e *Engine) listRemoteFiles(ctx context.Context) error {
	if e.db.IsListingComplete() {
		e.logger.Info("using cached file list")
		return nil
	}

	e.isListing.Store(true)
	defer e.isListing.Store(false)

	e.logger.Info("listing remote files...")

	var count int
	err := e.rclone.StreamListRecursive(ctx, func(rf *rclone.RemoteFile) error {
		count++
		e.listingFiles.Store(int32(count))

		if count%100 == 0 {
			e.logger.Info("listing progress", "files", count)
		}

		return e.db.SaveRemoteFile(&db.RemoteFile{
			Path:       rf.Path,
			Name:       rf.Name,
			Size:       rf.Size,
			ModTime:    rf.ParsedModTime(),
			MimeType:   rf.MimeType,
			FileID:     rf.ID,
			Downloaded: false,
		})
	})

	if err != nil {
		return err
	}

	e.logger.Info("listed remote files", "count", count)
	return e.db.SetListingComplete(count)
}

func (e *Engine) scanLocalFiles() (map[string]os.FileInfo, error) {
	files := make(map[string]os.FileInfo)

	err := filepath.WalkDir(e.localPath, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() {
			return nil
		}

		relPath, err := filepath.Rel(e.localPath, path)
		if err != nil {
			return nil
		}

		info, err := d.Info()
		if err != nil {
			return nil
		}

		files[relPath] = info
		return nil
	})

	return files, err
}

func (e *Engine) downloadFiles(ctx context.Context, files []*db.RemoteFile) error {
	e.isDownloading.Store(true)
	defer e.isDownloading.Store(false)

	e.downloadTotal.Store(int32(len(files)))
	e.downloadDone.Store(0)

	// Use a WaitGroup to track all downloads
	var wg sync.WaitGroup

	for _, rf := range files {
		if e.IsPaused() {
			break
		}

		select {
		case <-ctx.Done():
			break
		default:
		}

		// Acquire semaphore (limit concurrent downloads)
		select {
		case e.downloadSem <- struct{}{}:
		case <-ctx.Done():
			break
		}

		wg.Add(1)
		go func(rf *db.RemoteFile) {
			defer wg.Done()
			defer func() { <-e.downloadSem }() // Release semaphore

			localPath := filepath.Join(e.localPath, rf.Path)
			e.setCurrentFile(rf.Path)

			// Track this download to ignore file watcher events
			e.activeDownloads.Store(localPath, true)
			defer e.activeDownloads.Delete(localPath)

			// Create parent directory
			if err := os.MkdirAll(filepath.Dir(localPath), 0755); err != nil {
				e.logger.Error("failed to create directory", "path", localPath, "error", err)
				return
			}

			// Download
			if err := e.rclone.Download(ctx, rf.Path, localPath); err != nil {
				e.logger.Error("download failed", "path", rf.Path, "error", err)
				e.db.AddSyncHistory("download", localPath, "failed")
				return
			}

			// Mark as downloaded
			e.db.MarkRemoteFileDownloaded(rf.Path)
			e.db.MarkSynced(rf.Path, rf.ModTime, rf.Size)
			e.db.AddSyncHistory("download", localPath, "success")

			e.downloadDone.Add(1)
			e.logger.Debug("downloaded", "path", rf.Path)
		}(rf)
	}

	wg.Wait()
	e.setCurrentFile("")
	return nil
}

func (e *Engine) uploadNewFiles(ctx context.Context, localFiles map[string]os.FileInfo) error {
	e.isUploading.Store(true)
	defer e.isUploading.Store(false)

	var toUpload []string

	for relPath, info := range localFiles {
		state, _ := e.db.GetFileState(relPath)
		if state == nil {
			// File not in database, check if it's new
			toUpload = append(toUpload, relPath)
		} else if state.Status == db.StatusPendingUpload {
			toUpload = append(toUpload, relPath)
		} else if info.ModTime().Unix() > int64(state.LocalMTime) {
			// File modified since last sync
			toUpload = append(toUpload, relPath)
		}
	}

	if len(toUpload) == 0 {
		return nil
	}

	e.uploadTotal.Store(int32(len(toUpload)))
	e.uploadDone.Store(0)

	e.logger.Info("uploading files", "count", len(toUpload))

	for _, relPath := range toUpload {
		if e.IsPaused() {
			return nil
		}

		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}

		localPath := filepath.Join(e.localPath, relPath)
		e.setCurrentFile(relPath)

		info, err := os.Stat(localPath)
		if err != nil {
			continue
		}

		if err := e.rclone.Upload(ctx, localPath, relPath); err != nil {
			e.logger.Error("upload failed", "path", relPath, "error", err)
			e.db.AddSyncHistory("upload", localPath, "failed")
			continue
		}

		e.db.MarkSynced(relPath, float64(info.ModTime().Unix()), info.Size())
		e.db.AddSyncHistory("upload", localPath, "success")

		e.uploadDone.Add(1)
		e.logger.Debug("uploaded", "path", relPath)
	}

	e.setCurrentFile("")
	return nil
}

func (e *Engine) handleFileEvents(ctx context.Context) {
	debounce := time.NewTimer(time.Second)
	debounce.Stop()
	pendingSync := false

	for {
		select {
		case <-ctx.Done():
			return
		case event, ok := <-e.watcher.Events():
			if !ok {
				return
			}

			// Ignore events for files we're actively downloading
			if _, downloading := e.activeDownloads.Load(event.Path); downloading {
				e.logger.Debug("ignoring event for active download", "path", event.Path)
				continue
			}

			e.logger.Debug("file event", "path", event.Path, "type", event.Type)
			pendingSync = true
			debounce.Reset(2 * time.Second)

		case <-debounce.C:
			if pendingSync && !e.syncRunning.Load() {
				pendingSync = false
				go func() {
					if err := e.Sync(ctx); err != nil {
						e.logger.Error("sync after file event failed", "error", err)
					}
				}()
			}
		}
	}
}

func (e *Engine) scheduleNextSync(ctx context.Context) {
	e.syncMu.Lock()
	defer e.syncMu.Unlock()

	if e.syncTimer != nil {
		e.syncTimer.Stop()
	}

	e.syncTimer = time.AfterFunc(e.syncInterval, func() {
		if !e.IsPaused() {
			if err := e.Sync(ctx); err != nil {
				e.logger.Error("periodic sync failed", "error", err)
			}
		}
		e.scheduleNextSync(ctx)
	})
}
