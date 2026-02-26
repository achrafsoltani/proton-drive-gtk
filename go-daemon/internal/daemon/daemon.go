// Package daemon provides the main daemon orchestration.
package daemon

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/achrafsoltani/proton-drive-gtk/go-daemon/internal/config"
	"github.com/achrafsoltani/proton-drive-gtk/go-daemon/internal/db"
	"github.com/achrafsoltani/proton-drive-gtk/go-daemon/internal/ipc"
	"github.com/achrafsoltani/proton-drive-gtk/go-daemon/internal/sync"
	"github.com/achrafsoltani/proton-drive-gtk/go-daemon/internal/watcher"
)

// Daemon is the main sync daemon.
type Daemon struct {
	config  *config.Config
	paths   *config.Paths
	db      *db.StateDB
	watcher *watcher.Watcher
	engine  *sync.Engine
	ipc     *ipc.Server
	logger  *slog.Logger

	ctx    context.Context
	cancel context.CancelFunc
}

// New creates a new daemon.
func New(cfg *config.Config, logger *slog.Logger) (*Daemon, error) {
	paths := config.GetPaths()

	// Ensure directories exist
	if err := os.MkdirAll(paths.CacheDir, 0755); err != nil {
		return nil, fmt.Errorf("failed to create cache directory: %w", err)
	}

	// Ensure local path exists
	if err := os.MkdirAll(cfg.LocalPath, 0755); err != nil {
		return nil, fmt.Errorf("failed to create local path: %w", err)
	}

	// Open database
	stateDB, err := db.Open(paths.DBPath)
	if err != nil {
		return nil, fmt.Errorf("failed to open database: %w", err)
	}

	// Create file watcher
	w, err := watcher.New(cfg.LocalPath, 1000, logger, cfg.ExcludePatterns)
	if err != nil {
		stateDB.Close()
		return nil, fmt.Errorf("failed to create watcher: %w", err)
	}

	// Create sync engine
	engine := sync.NewEngine(cfg.LocalPath, cfg.RemoteName, stateDB, w, logger, cfg.MaxConcurrentTransfers, cfg.ExcludePatterns)

	// Create IPC server
	ipcServer, err := ipc.NewServer(paths.SocketPath, engine, logger)
	if err != nil {
		w.Stop()
		stateDB.Close()
		return nil, fmt.Errorf("failed to create IPC server: %w", err)
	}

	ctx, cancel := context.WithCancel(context.Background())

	return &Daemon{
		config:  cfg,
		paths:   paths,
		db:      stateDB,
		watcher: w,
		engine:  engine,
		ipc:     ipcServer,
		logger:  logger,
		ctx:     ctx,
		cancel:  cancel,
	}, nil
}

// Run starts the daemon and blocks until shutdown.
func (d *Daemon) Run() error {
	d.logger.Info("starting daemon",
		"local_path", d.config.LocalPath,
		"remote", d.config.RemoteName,
		"socket", d.paths.SocketPath,
	)

	// Start IPC server
	go func() {
		if err := d.ipc.Serve(d.ctx); err != nil {
			d.logger.Error("IPC server error", "error", err)
		}
	}()

	// Start sync engine
	if err := d.engine.Start(d.ctx); err != nil {
		return fmt.Errorf("failed to start sync engine: %w", err)
	}

	// Handle signals
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM, syscall.SIGUSR1, syscall.SIGUSR2)

	d.logger.Info("daemon started")

	for {
		select {
		case sig := <-sigCh:
			switch sig {
			case syscall.SIGUSR1:
				d.logger.Info("received SIGUSR1, pausing sync")
				d.engine.Pause()
			case syscall.SIGUSR2:
				d.logger.Info("received SIGUSR2, resuming sync")
				d.engine.Resume()
			case syscall.SIGINT, syscall.SIGTERM:
				d.logger.Info("received shutdown signal")
				d.Shutdown()
				return nil
			}
		case <-d.ctx.Done():
			return nil
		}
	}
}

// Shutdown gracefully shuts down the daemon.
func (d *Daemon) Shutdown() {
	d.logger.Info("shutting down daemon")

	d.cancel()

	if d.engine != nil {
		d.engine.Stop()
	}

	if d.ipc != nil {
		d.ipc.Close()
	}

	if d.watcher != nil {
		d.watcher.Stop()
	}

	if d.db != nil {
		d.db.Close()
	}

	d.logger.Info("daemon stopped")
}

// GetStats returns daemon statistics.
func (d *Daemon) GetStats() sync.Stats {
	return d.engine.GetStats()
}
