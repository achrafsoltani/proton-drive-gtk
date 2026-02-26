// Package watcher provides file system watching using inotify.
package watcher

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/fsnotify/fsnotify"
)

// EventType represents the type of file event.
type EventType int

const (
	EventCreate EventType = iota
	EventModify
	EventDelete
	EventRename
)

func (e EventType) String() string {
	switch e {
	case EventCreate:
		return "create"
	case EventModify:
		return "modify"
	case EventDelete:
		return "delete"
	case EventRename:
		return "rename"
	default:
		return "unknown"
	}
}

// FileEvent represents a file system event.
type FileEvent struct {
	Path      string
	Type      EventType
	Timestamp time.Time
}

// Watcher watches a directory tree for file changes.
type Watcher struct {
	watcher        *fsnotify.Watcher
	rootPath       string
	events         chan FileEvent
	ignorePatterns []string
	logger         *slog.Logger

	// Debouncing
	debounceTime time.Duration
	pendingMu    sync.Mutex
	pending      map[string]*pendingEvent
	debounceStop chan struct{}
	stopOnce     sync.Once
}

type pendingEvent struct {
	event FileEvent
	timer *time.Timer
}

// New creates a new file watcher.
func New(rootPath string, bufferSize int, logger *slog.Logger) (*Watcher, error) {
	w, err := fsnotify.NewWatcher()
	if err != nil {
		return nil, err
	}

	return &Watcher{
		watcher:  w,
		rootPath: rootPath,
		events:   make(chan FileEvent, bufferSize),
		ignorePatterns: []string{
			".git", ".DS_Store", "*.tmp", "*.swp", "*.swx",
			"*~", ".~*", "*.part", "*.crdownload", ".rclone*",
		},
		logger:       logger,
		debounceTime: time.Second,
		pending:      make(map[string]*pendingEvent),
		debounceStop: make(chan struct{}),
	}, nil
}

// Start starts watching the directory tree.
func (w *Watcher) Start(ctx context.Context) error {
	// Add watches recursively
	err := filepath.WalkDir(w.rootPath, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return nil // Skip errors
		}
		if d.IsDir() && !w.shouldIgnore(path) {
			if err := w.watcher.Add(path); err != nil {
				w.logger.Warn("failed to watch directory", "path", path, "error", err)
			}
		}
		return nil
	})
	if err != nil {
		return err
	}

	go w.processEvents(ctx)
	w.logger.Info("file watcher started", "path", w.rootPath)
	return nil
}

// Stop stops the watcher.
func (w *Watcher) Stop() error {
	w.stopOnce.Do(func() {
		close(w.debounceStop)
	})
	return w.watcher.Close()
}

// Events returns the channel of file events.
func (w *Watcher) Events() <-chan FileEvent {
	return w.events
}

func (w *Watcher) shouldIgnore(path string) bool {
	name := filepath.Base(path)
	for _, pattern := range w.ignorePatterns {
		if matched, _ := filepath.Match(pattern, name); matched {
			return true
		}
		// Also check if pattern is in path
		if strings.Contains(path, pattern) {
			return true
		}
	}
	return false
}

func (w *Watcher) processEvents(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case <-w.debounceStop:
			return
		case event, ok := <-w.watcher.Events:
			if !ok {
				return
			}
			w.handleFsEvent(event)
		case err, ok := <-w.watcher.Errors:
			if !ok {
				return
			}
			w.logger.Error("watcher error", "error", err)
		}
	}
}

func (w *Watcher) handleFsEvent(event fsnotify.Event) {
	if w.shouldIgnore(event.Name) {
		return
	}

	var eventType EventType
	switch {
	case event.Op&fsnotify.Create == fsnotify.Create:
		eventType = EventCreate
		// Add watch for new directories
		if info, err := os.Stat(event.Name); err == nil && info.IsDir() {
			w.watcher.Add(event.Name)
		}
	case event.Op&fsnotify.Write == fsnotify.Write:
		eventType = EventModify
	case event.Op&fsnotify.Remove == fsnotify.Remove:
		eventType = EventDelete
	case event.Op&fsnotify.Rename == fsnotify.Rename:
		eventType = EventRename
	default:
		return
	}

	fe := FileEvent{
		Path:      event.Name,
		Type:      eventType,
		Timestamp: time.Now(),
	}

	w.queueEvent(fe)
}

func (w *Watcher) queueEvent(fe FileEvent) {
	w.pendingMu.Lock()
	defer w.pendingMu.Unlock()

	// Cancel existing timer for this path
	if existing, ok := w.pending[fe.Path]; ok {
		existing.timer.Stop()
	}

	// Create new timer
	timer := time.AfterFunc(w.debounceTime, func() {
		w.flushEvent(fe.Path)
	})

	w.pending[fe.Path] = &pendingEvent{
		event: fe,
		timer: timer,
	}
}

func (w *Watcher) flushEvent(path string) {
	w.pendingMu.Lock()
	pe, ok := w.pending[path]
	if ok {
		delete(w.pending, path)
	}
	w.pendingMu.Unlock()

	if ok {
		select {
		case w.events <- pe.event:
			w.logger.Debug("file event", "path", pe.event.Path, "type", pe.event.Type)
		default:
			w.logger.Warn("event channel full, dropping event", "path", pe.event.Path)
		}
	}
}
