VERSION ?= $(shell sed -n 's/^var Version = "\(.*\)"/\1/p' internal/config/config.go)
BINARY  := cfassist
PKG     := ./cmd/cfassist
LDFLAGS := -s -w -X github.com/aachtenberg/cfoperator/cfassist-go/internal/config.Version=$(VERSION)

.PHONY: build clean all sync-skills

# The nine playbooks are embedded in the binary (internal/skills), so a cfassist
# curl'd onto a bare Pi has them with no network. go:embed cannot cross the
# module boundary, so they are copied in rather than referenced; test_skills_bundle.py
# fails if this copy drifts from ../skills.
# Only SKILL.md, not the directories wholesale: `go:embed all:bundled` bakes in
# whatever is here, and the embed surface should equal what skills.Load actually
# reads. A fixture or a stray .pyc appearing in ../skills must not ship inside
# the binary.
sync-skills:
	rm -rf internal/skills/bundled
	@for skill in ../skills/*/SKILL.md; do \
		name=$$(basename $$(dirname $$skill)); \
		mkdir -p internal/skills/bundled/$$name; \
		cp $$skill internal/skills/bundled/$$name/SKILL.md; \
	done

build:
	go build -ldflags "$(LDFLAGS)" -o $(BINARY) $(PKG)

# Cross-compile targets
linux-amd64:
	GOOS=linux GOARCH=amd64 go build -ldflags "$(LDFLAGS)" -o $(BINARY)-linux-amd64 $(PKG)

linux-arm:
	GOOS=linux GOARCH=arm GOARM=7 go build -ldflags "$(LDFLAGS)" -o $(BINARY)-linux-arm $(PKG)

linux-arm64:
	GOOS=linux GOARCH=arm64 go build -ldflags "$(LDFLAGS)" -o $(BINARY)-linux-arm64 $(PKG)

darwin-amd64:
	GOOS=darwin GOARCH=amd64 go build -ldflags "$(LDFLAGS)" -o $(BINARY)-darwin-amd64 $(PKG)

darwin-arm64:
	GOOS=darwin GOARCH=arm64 go build -ldflags "$(LDFLAGS)" -o $(BINARY)-darwin-arm64 $(PKG)

all: linux-amd64 linux-arm linux-arm64 darwin-amd64 darwin-arm64

clean:
	rm -f $(BINARY) $(BINARY)-*
