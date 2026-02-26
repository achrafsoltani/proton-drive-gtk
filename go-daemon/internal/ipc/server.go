// Package ipc provides Unix socket IPC for communication with the tray application.
package ipc

import (
	"bufio"
	"context"
	"fmt"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/achrafsoltani/proton-drive-gtk/go-daemon/internal/sync"
)

// Server is the IPC server.
type Server struct {
	listener   net.Listener
	socketPath string
	engine     *sync.Engine
	logger     *slog.Logger
}

// NewServer creates a new IPC server.
func NewServer(socketPath string, engine *sync.Engine, logger *slog.Logger) (*Server, error) {
	// Ensure directory exists
	if err := os.MkdirAll(filepath.Dir(socketPath), 0755); err != nil {
		return nil, err
	}

	// Remove existing socket
	os.Remove(socketPath)

	listener, err := net.Listen("unix", socketPath)
	if err != nil {
		return nil, err
	}

	return &Server{
		listener:   listener,
		socketPath: socketPath,
		engine:     engine,
		logger:     logger,
	}, nil
}

// Serve starts accepting connections.
func (s *Server) Serve(ctx context.Context) error {
	s.logger.Info("IPC server started", "socket", s.socketPath)

	go func() {
		<-ctx.Done()
		s.listener.Close()
	}()

	for {
		conn, err := s.listener.Accept()
		if err != nil {
			select {
			case <-ctx.Done():
				return nil
			default:
				s.logger.Error("accept error", "error", err)
				continue
			}
		}

		go s.handleConnection(conn)
	}
}

// Close closes the server.
func (s *Server) Close() error {
	os.Remove(s.socketPath)
	return s.listener.Close()
}

func (s *Server) handleConnection(conn net.Conn) {
	defer conn.Close()

	scanner := bufio.NewScanner(conn)
	var lines []string

	for scanner.Scan() {
		line := scanner.Text()
		if line == "done" {
			break
		}
		lines = append(lines, line)
	}

	if err := scanner.Err(); err != nil {
		s.logger.Error("read error", "error", err)
		return
	}

	response := s.processRequest(lines)
	conn.Write([]byte(response))
}

func (s *Server) processRequest(lines []string) string {
	if len(lines) == 0 {
		return "error\nmessage\tEmpty request\ndone\n"
	}

	command := strings.ToUpper(strings.TrimSpace(lines[0]))

	switch command {
	case "PING":
		return "ok\npong\ndone\n"

	case "STATUS":
		return s.handleStatus(lines[1:])

	case "STATS":
		return s.handleStats()

	case "SYNC":
		return s.handleSync()

	case "PAUSE":
		return s.handlePause()

	case "RESUME":
		return s.handleResume()

	case "CLEAR_CACHE":
		return s.handleClearCache()

	default:
		return fmt.Sprintf("error\nmessage\tUnknown command: %s\ndone\n", command)
	}
}

func (s *Server) handleStatus(lines []string) string {
	// Parse path from lines
	var path string
	for _, line := range lines {
		if strings.HasPrefix(line, "path\t") {
			path = strings.TrimPrefix(line, "path\t")
			break
		}
	}

	if path == "" {
		return "error\nmessage\tNo path provided\ndone\n"
	}

	status, err := s.engine.GetStatus(path)
	if err != nil {
		return fmt.Sprintf("error\nmessage\t%s\ndone\n", err.Error())
	}

	return fmt.Sprintf("ok\nstatus\t%s\ndone\n", status)
}

func (s *Server) handleStats() string {
	stats := s.engine.GetStats()

	var sb strings.Builder
	sb.WriteString("ok\n")
	sb.WriteString(fmt.Sprintf("status\t%s\n", stats.Status))
	sb.WriteString(fmt.Sprintf("total_files\t%d\n", stats.TotalFiles))
	sb.WriteString(fmt.Sprintf("synced_files\t%d\n", stats.SyncedFiles))
	sb.WriteString(fmt.Sprintf("pending_upload\t%d\n", stats.PendingUpload))
	sb.WriteString(fmt.Sprintf("pending_download\t%d\n", stats.PendingDownload))
	sb.WriteString(fmt.Sprintf("errors\t%d\n", stats.Errors))

	if stats.CurrentFile != "" {
		sb.WriteString(fmt.Sprintf("current_file\t%s\n", stats.CurrentFile))
	}

	sb.WriteString(fmt.Sprintf("is_listing\t%s\n", boolToStr(stats.IsListing)))
	sb.WriteString(fmt.Sprintf("is_downloading\t%s\n", boolToStr(stats.IsDownloading)))
	sb.WriteString(fmt.Sprintf("is_uploading\t%s\n", boolToStr(stats.IsUploading)))
	sb.WriteString(fmt.Sprintf("download_total\t%d\n", stats.DownloadTotal))
	sb.WriteString(fmt.Sprintf("download_done\t%d\n", stats.DownloadDone))
	sb.WriteString(fmt.Sprintf("upload_total\t%d\n", stats.UploadTotal))
	sb.WriteString(fmt.Sprintf("upload_done\t%d\n", stats.UploadDone))

	if stats.ETASeconds > 0 {
		sb.WriteString(fmt.Sprintf("eta_seconds\t%d\n", stats.ETASeconds))
	}

	sb.WriteString("done\n")
	return sb.String()
}

func (s *Server) handleSync() string {
	go func() {
		if err := s.engine.ForceSync(context.Background()); err != nil {
			s.logger.Error("force sync failed", "error", err)
		}
	}()
	return "ok\ndone\n"
}

func (s *Server) handlePause() string {
	s.engine.Pause()
	return "ok\ndone\n"
}

func (s *Server) handleResume() string {
	s.engine.Resume()
	return "ok\ndone\n"
}

func (s *Server) handleClearCache() string {
	if err := s.engine.ClearCache(); err != nil {
		return fmt.Sprintf("error\nmessage\t%s\ndone\n", err.Error())
	}
	return "ok\ndone\n"
}

func boolToStr(b bool) string {
	if b {
		return "1"
	}
	return "0"
}

// ParseResponse parses an IPC response into a map.
func ParseResponse(text string) map[string]string {
	result := make(map[string]string)
	for _, line := range strings.Split(strings.TrimSpace(text), "\n") {
		if strings.Contains(line, "\t") {
			parts := strings.SplitN(line, "\t", 2)
			result[parts[0]] = parts[1]
		} else if line == "ok" {
			result["ok"] = "true"
		} else if line == "error" {
			result["error"] = "true"
		}
	}
	return result
}

// ParseInt parses an int from a response value.
func ParseInt(val string) int {
	i, _ := strconv.Atoi(val)
	return i
}

// ParseBool parses a bool from a response value.
func ParseBool(val string) bool {
	return val == "1" || val == "true"
}
