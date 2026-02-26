// Package config handles configuration loading for the sync daemon.
package config

import (
	"encoding/json"
	"os"
	"path/filepath"
)

// Config holds the daemon configuration.
type Config struct {
	RemoteName             string `json:"remote_name"`
	LocalPath              string `json:"mount_path"`
	SyncInterval           int    `json:"sync_interval"`
	ConflictResolution     string `json:"conflict_resolution"`
	ShowNotifications      bool   `json:"show_notifications"`
	StartOnLogin           bool   `json:"start_on_login"`
	MaxConcurrentTransfers int    `json:"max_concurrent_transfers"`
}

// Paths returns common paths used by the daemon.
type Paths struct {
	ConfigDir  string
	ConfigFile string
	CacheDir   string
	DBPath     string
	SocketPath string
}

// DefaultConfig returns the default configuration.
func DefaultConfig() *Config {
	home, _ := os.UserHomeDir()
	return &Config{
		RemoteName:             "protondrive",
		LocalPath:              filepath.Join(home, "ProtonDrive"),
		SyncInterval:           60,
		ConflictResolution:     "newer",
		ShowNotifications:      true,
		StartOnLogin:           false,
		MaxConcurrentTransfers: 4,
	}
}

// GetPaths returns the standard paths for the daemon.
func GetPaths() *Paths {
	home, _ := os.UserHomeDir()
	configDir := filepath.Join(home, ".config", "proton-drive-gtk")
	cacheDir := filepath.Join(home, ".cache", "proton-drive-gtk")

	return &Paths{
		ConfigDir:  configDir,
		ConfigFile: filepath.Join(configDir, "config.json"),
		CacheDir:   cacheDir,
		DBPath:     filepath.Join(cacheDir, "sync_state.db"),
		SocketPath: filepath.Join(cacheDir, "daemon.sock"),
	}
}

// Load loads the configuration from disk.
func Load() (*Config, error) {
	paths := GetPaths()

	data, err := os.ReadFile(paths.ConfigFile)
	if err != nil {
		if os.IsNotExist(err) {
			return DefaultConfig(), nil
		}
		return nil, err
	}

	cfg := DefaultConfig()
	if err := json.Unmarshal(data, cfg); err != nil {
		return nil, err
	}

	return cfg, nil
}

// Save saves the configuration to disk.
func (c *Config) Save() error {
	paths := GetPaths()

	if err := os.MkdirAll(paths.ConfigDir, 0755); err != nil {
		return err
	}

	data, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return err
	}

	return os.WriteFile(paths.ConfigFile, data, 0644)
}
