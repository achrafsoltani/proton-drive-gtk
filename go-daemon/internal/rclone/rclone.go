// Package rclone provides a wrapper for rclone commands.
package rclone

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os/exec"
	"strings"
	"time"
)

// RemoteFile represents a file from rclone lsjson output.
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

// Client wraps rclone commands.
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

// ListDir lists a directory on the remote.
func (c *Client) ListDir(ctx context.Context, path string) ([]*RemoteFile, error) {
	remotePath := c.remoteName + ":"
	if path != "" {
		remotePath += path
	}

	cmd := exec.CommandContext(ctx, "rclone", "lsjson", remotePath)
	output, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("rclone lsjson failed: %w", err)
	}

	var files []*RemoteFile
	if err := json.Unmarshal(output, &files); err != nil {
		return nil, fmt.Errorf("failed to parse rclone output: %w", err)
	}

	return files, nil
}

// StreamListRecursive streams all files recursively, calling handler for each file.
// This avoids loading all files into memory at once.
func (c *Client) StreamListRecursive(ctx context.Context, handler func(*RemoteFile) error) error {
	cmd := exec.CommandContext(ctx, "rclone", "lsjson", "-R", c.remoteName+":")
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return fmt.Errorf("failed to create stdout pipe: %w", err)
	}

	if err := cmd.Start(); err != nil {
		return fmt.Errorf("failed to start rclone: %w", err)
	}

	decoder := json.NewDecoder(stdout)

	// Read opening bracket
	if _, err := decoder.Token(); err != nil {
		cmd.Process.Kill()
		return fmt.Errorf("failed to read JSON start: %w", err)
	}

	// Stream each file
	for decoder.More() {
		var rf RemoteFile
		if err := decoder.Decode(&rf); err != nil {
			c.logger.Warn("failed to decode file entry", "error", err)
			continue
		}

		if !rf.IsDir {
			if err := handler(&rf); err != nil {
				cmd.Process.Kill()
				return err
			}
		}
	}

	return cmd.Wait()
}

// Download downloads a file from remote to local.
func (c *Client) Download(ctx context.Context, remotePath, localPath string) error {
	src := c.remoteName + ":" + remotePath
	cmd := exec.CommandContext(ctx, "rclone", "copyto", src, localPath)
	output, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("download failed: %s: %w", string(output), err)
	}
	return nil
}

// Upload uploads a file from local to remote.
func (c *Client) Upload(ctx context.Context, localPath, remotePath string) error {
	dst := c.remoteName + ":" + remotePath
	cmd := exec.CommandContext(ctx, "rclone", "copyto",
		"--protondrive-replace-existing-draft=true",
		localPath, dst)
	output, err := cmd.CombinedOutput()
	if err != nil {
		// Check if file already exists (not really an error)
		if strings.Contains(strings.ToLower(string(output)), "already exists") {
			c.logger.Info("file already exists on remote", "path", remotePath)
			return nil
		}
		return fmt.Errorf("upload failed: %s: %w", string(output), err)
	}
	return nil
}

// Delete deletes a file on the remote.
func (c *Client) Delete(ctx context.Context, remotePath string) error {
	path := c.remoteName + ":" + remotePath
	cmd := exec.CommandContext(ctx, "rclone", "deletefile", path)
	output, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("delete failed: %s: %w", string(output), err)
	}
	return nil
}

// Mkdir creates a directory on the remote.
func (c *Client) Mkdir(ctx context.Context, remotePath string) error {
	path := c.remoteName + ":" + remotePath
	cmd := exec.CommandContext(ctx, "rclone", "mkdir", path)
	output, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("mkdir failed: %s: %w", string(output), err)
	}
	return nil
}

// CheckRemote checks if the remote is configured and accessible.
func (c *Client) CheckRemote(ctx context.Context) error {
	// Check if remote exists
	cmd := exec.CommandContext(ctx, "rclone", "listremotes")
	output, err := cmd.Output()
	if err != nil {
		return fmt.Errorf("failed to list remotes: %w", err)
	}

	if !strings.Contains(string(output), c.remoteName+":") {
		return fmt.Errorf("remote %q not configured", c.remoteName)
	}

	// Try to access the remote
	cmd = exec.CommandContext(ctx, "rclone", "lsd", c.remoteName+":")
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("failed to access remote: %w", err)
	}

	return nil
}

// GetVersion returns the rclone version.
func GetVersion(ctx context.Context) (string, error) {
	cmd := exec.CommandContext(ctx, "rclone", "version")
	output, err := cmd.Output()
	if err != nil {
		return "", err
	}

	scanner := bufio.NewScanner(strings.NewReader(string(output)))
	if scanner.Scan() {
		return scanner.Text(), nil
	}
	return "", fmt.Errorf("no version output")
}
