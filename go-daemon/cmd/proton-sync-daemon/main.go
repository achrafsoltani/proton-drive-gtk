// Command proton-sync-daemon is the Proton Drive sync daemon.
package main

import (
	"flag"
	"fmt"
	"log/slog"
	"os"

	"github.com/achrafsoltani/proton-drive-gtk/go-daemon/internal/config"
	"github.com/achrafsoltani/proton-drive-gtk/go-daemon/internal/daemon"
	"github.com/achrafsoltani/proton-drive-gtk/go-daemon/internal/rclone"
)

var (
	Version = "dev"
)

func main() {
	var (
		showVersion  = flag.Bool("version", false, "Show version")
		debug        = flag.Bool("debug", false, "Enable debug logging")
		localPath    = flag.String("local", "", "Local sync path (overrides config)")
		remoteName   = flag.String("remote", "", "Remote name (overrides config)")
		maxTransfers = flag.Int("max-transfers", 4, "Max concurrent uploads/downloads")
	)
	flag.Parse()

	if *showVersion {
		fmt.Printf("proton-sync-daemon %s\n", Version)
		os.Exit(0)
	}

	// Setup logging
	level := slog.LevelInfo
	if *debug {
		level = slog.LevelDebug
	}
	logger := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{
		Level: level,
	}))
	slog.SetDefault(logger)

	// Initialise embedded rclone
	rclone.Initialize()
	defer rclone.Finalize()

	version, err := rclone.GetVersion()
	if err != nil {
		logger.Error("failed to get embedded rclone version", "error", err)
		os.Exit(1)
	}
	logger.Info("embedded rclone ready", "version", version)

	// Load config
	cfg, err := config.Load()
	if err != nil {
		logger.Error("failed to load config", "error", err)
		os.Exit(1)
	}

	// Override config from flags
	if *localPath != "" {
		cfg.LocalPath = *localPath
	}
	if *remoteName != "" {
		cfg.RemoteName = *remoteName
	}
	if *maxTransfers > 0 {
		cfg.MaxConcurrentTransfers = *maxTransfers
	}

	// Create and run daemon
	d, err := daemon.New(cfg, logger)
	if err != nil {
		logger.Error("failed to create daemon", "error", err)
		os.Exit(1)
	}

	if err := d.Run(); err != nil {
		logger.Error("daemon error", "error", err)
		os.Exit(1)
	}
}
