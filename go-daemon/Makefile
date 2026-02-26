BINARY=proton-sync-daemon
VERSION=$(shell git describe --tags --always --dirty 2>/dev/null || echo "dev")
BUILD_FLAGS=-ldflags "-X main.Version=$(VERSION)"
GO=go

.PHONY: all build clean test fmt lint install

all: build

build:
	$(GO) build $(BUILD_FLAGS) -o bin/$(BINARY) ./cmd/proton-sync-daemon

linux-amd64:
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 $(GO) build $(BUILD_FLAGS) -o bin/$(BINARY)-linux-amd64 ./cmd/proton-sync-daemon

linux-arm64:
	CGO_ENABLED=0 GOOS=linux GOARCH=arm64 $(GO) build $(BUILD_FLAGS) -o bin/$(BINARY)-linux-arm64 ./cmd/proton-sync-daemon

test:
	$(GO) test -v ./...

fmt:
	$(GO) fmt ./...

lint:
	golangci-lint run

clean:
	rm -rf bin/

install: build
	install -m 755 bin/$(BINARY) /usr/local/bin/

deps:
	$(GO) mod download
	$(GO) mod tidy
