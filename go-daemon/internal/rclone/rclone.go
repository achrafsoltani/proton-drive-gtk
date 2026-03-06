// Package rclone provides in-process access to rclone via librclone.
package rclone

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"path/filepath"
	"strings"
	"time"

	"github.com/rclone/rclone/librclone/librclone"

	_ "github.com/rclone/rclone/backend/local"       // register local filesystem backend
	_ "github.com/rclone/rclone/backend/protondrive" // register Proton Drive backend
	_ "github.com/rclone/rclone/fs/operations"  // register operations/* RC commands
	_ "github.com/rclone/rclone/fs/config"      // register config/* RC commands
)

// Initialize initialises rclone as an in-process library.
// Must be called once before any Client methods.
func Initialize() {
	librclone.Initialize()
}

// Finalize releases rclone resources. Call on shutdown.
func Finalize() {
	librclone.Finalize()
}

// RemoteFile represents a file from rclone list output.
type RemoteFile struct {
	Path     string `json:"Path"`
	Name     string `json:"Name"`
	Size     int64  `json:"Size"`
	MimeType string `json:"MimeType"`
	ModTime  string `json:"ModTime"`
	IsDir    bool   `json:"IsDir"`
	ID       string `json:"ID"`
}

// ParsedModTime returns the mod time as a Unix timestamp.
func (rf *RemoteFile) ParsedModTime() float64 {
	t, err := time.Parse(time.RFC3339Nano, rf.ModTime)
	if err != nil {
		return 0
	}
	return float64(t.Unix())
}

// Client wraps rclone operations via librclone RPC.
type Client struct {
	remoteName string
	logger     *slog.Logger
}

// NewClient creates a new rclone client.
func NewClient(remoteName string, logger *slog.Logger) *Client {
	return &Client{
		remoteName: remoteName,
		logger:     logger,
	}
}

// StreamListRecursive lists all files recursively, calling handler for each file.
func (c *Client) StreamListRecursive(_ context.Context, handler func(*RemoteFile) error) error {
	params := map[string]interface{}{
		"fs":     c.remoteName + ":",
		"remote": "",
		"opt": map[string]interface{}{
			"recurse":   true,
			"filesOnly": true,
		},
	}

	out, status := librclone.RPC("operations/list", mustJSON(params))
	if status != 200 {
		return fmt.Errorf("list failed (status %d): %s", status, out)
	}

	var result struct {
		List []*RemoteFile `json:"list"`
	}
	if err := json.Unmarshal([]byte(out), &result); err != nil {
		return fmt.Errorf("failed to parse list: %w", err)
	}

	for _, rf := range result.List {
		if err := handler(rf); err != nil {
			return err
		}
	}

	return nil
}

// Download downloads a file from remote to local.
func (c *Client) Download(_ context.Context, remotePath, localPath string) error {
	params := map[string]interface{}{
		"srcFs":     c.remoteName + ":",
		"srcRemote": remotePath,
		"dstFs":     filepath.Dir(localPath),
		"dstRemote": filepath.Base(localPath),
	}

	out, status := librclone.RPC("operations/copyfile", mustJSON(params))
	if status != 200 {
		return fmt.Errorf("download failed (status %d): %s", status, out)
	}
	return nil
}

// Upload uploads a file from local to remote.
func (c *Client) Upload(_ context.Context, localPath, remotePath string) error {
	params := map[string]interface{}{
		"srcFs":     filepath.Dir(localPath),
		"srcRemote": filepath.Base(localPath),
		"dstFs":     c.remoteName + ",replace_existing_draft=true:",
		"dstRemote": remotePath,
	}

	out, status := librclone.RPC("operations/copyfile", mustJSON(params))
	if status != 200 {
		outLower := strings.ToLower(out)
		if strings.Contains(outLower, "already exists") {
			c.logger.Info("file already exists on remote", "path", remotePath)
			return nil
		}
		return fmt.Errorf("upload failed (status %d): %s", status, out)
	}
	return nil
}

// Delete deletes a file on the remote.
func (c *Client) Delete(_ context.Context, remotePath string) error {
	params := map[string]interface{}{
		"fs":     c.remoteName + ":",
		"remote": remotePath,
	}

	out, status := librclone.RPC("operations/deletefile", mustJSON(params))
	if status != 200 {
		return fmt.Errorf("delete failed (status %d): %s", status, out)
	}
	return nil
}

// Mkdir creates a directory on the remote.
func (c *Client) Mkdir(_ context.Context, remotePath string) error {
	params := map[string]interface{}{
		"fs":     c.remoteName + ":",
		"remote": remotePath,
	}

	out, status := librclone.RPC("operations/mkdir", mustJSON(params))
	if status != 200 {
		return fmt.Errorf("mkdir failed (status %d): %s", status, out)
	}
	return nil
}

// CheckRemote checks if the remote is configured and accessible.
func (c *Client) CheckRemote(_ context.Context) error {
	out, status := librclone.RPC("config/listremotes", "{}")
	if status != 200 {
		return fmt.Errorf("failed to list remotes: %s", out)
	}

	var result struct {
		Remotes []string `json:"remotes"`
	}
	if err := json.Unmarshal([]byte(out), &result); err != nil {
		return fmt.Errorf("failed to parse remotes: %w", err)
	}

	found := false
	for _, r := range result.Remotes {
		if r == c.remoteName {
			found = true
			break
		}
	}
	if !found {
		return fmt.Errorf("remote %q not configured in rclone", c.remoteName)
	}

	// Try to access the remote
	listParams := map[string]interface{}{
		"fs":     c.remoteName + ":",
		"remote": "",
		"opt": map[string]interface{}{
			"dirsOnly": true,
		},
	}

	out, status = librclone.RPC("operations/list", mustJSON(listParams))
	if status != 200 {
		return fmt.Errorf("failed to access remote: %s", out)
	}

	return nil
}

// GetVersion returns the embedded rclone version.
func GetVersion() (string, error) {
	out, status := librclone.RPC("core/version", "{}")
	if status != 200 {
		return "", fmt.Errorf("failed to get version: %s", out)
	}

	var result struct {
		Version string `json:"version"`
	}
	if err := json.Unmarshal([]byte(out), &result); err != nil {
		return "", err
	}

	return "rclone " + result.Version, nil
}

func mustJSON(v interface{}) string {
	b, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return string(b)
}
