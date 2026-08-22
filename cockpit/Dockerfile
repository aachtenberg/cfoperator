# cfoperator-cockpit: the ephemeral *interactive* Job image (CFOP-35).
#
# The worker image is the base, not the executor one. executor/Dockerfile is
# deliberately stdlib-only — its own header says to build a separate variant if
# you need the CLI and kubectl — whereas worker/Dockerfile (node:22-slim +
# python + kubectl + openssh-client + claude-code, non-root uid 10001) already
# carries exactly the toolchain an operator wants under their hands during an
# incident. This image adds cfassist and swaps the entrypoint: the worker's is a
# batch script that runs `claude -p` and posts a report back, and a cockpit is
# the opposite of batch.
#
# The pod fetches its own briefing from inside the cluster using the
# per-investigation session token mounted from a short-lived Secret; nothing
# about the incident is baked into the image or the manifest.
#
# Multi-arch (amd64 + arm64): the affected node is frequently a Pi, and the
# whole point of tier 1 is landing on the affected node.

# cfassist is built from source rather than pulled from a GitHub release, and
# the reason has changed since this was written. Originally: no release
# implemented `attach` at all. Now they do — but building from the tree is the
# better property anyway, because it guarantees the in-pod CLI is on the same
# commit as the agent it talks to, which a floating release tag cannot promise.
#
# Consequence: this binary reports the tree's Version, which is NOT the same
# artifact as the release of that number. Tiers 3/3b (cockpit_ladder.py) DO
# fetch the release, so those two must be kept in step — bump
# internal/config/config.go and DEFAULT_CFASSIST_VERSION together, then tag.

# These two ARGs belong to the FROM lines below, not to the paragraph above
# them: a ${VAR} in FROM resolves only from a *global* ARG — one declared before
# the FIRST FROM. Both stay here for that reason, including WORKER_IMAGE, which
# feeds the second FROM: an ARG declared after any FROM is scoped to that stage
# and does nothing for a later base image, which fails identically and looks
# correct.
#
# They were lost once already, in a comment rewrite that replaced the block they
# happened to sit at the bottom of — GO_IMAGE went blank and the cockpit image
# stopped building for four merges without anything else noticing.
ARG GO_IMAGE=golang:1.25-bookworm
ARG WORKER_IMAGE=ghcr.io/aachtenberg/cfoperator-worker:main

# Cross-compiled on the build platform (Go does this natively), so the arm64
# image costs no QEMU emulation — the final stage below is COPY-only.
FROM --platform=$BUILDPLATFORM ${GO_IMAGE} AS cfassist-build
ARG TARGETARCH
WORKDIR /src
COPY cfassist-go/go.mod cfassist-go/go.sum ./
RUN go mod download
COPY cfassist-go/ ./
RUN CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH} \
    go build -ldflags "-s -w" -o /out/cfassist ./cmd/cfassist

FROM ${WORKER_IMAGE}

COPY --from=cfassist-build /out/cfassist /usr/local/bin/cfassist
COPY cockpit/entrypoint.sh /usr/local/bin/cockpit-entrypoint

# Inherited from the worker image; restated because this image's identity
# (uid 10001, non-root, no shell service account) is a security property and
# not an implementation detail of the base.
USER worker
WORKDIR /home/worker
# Explicit because cfassist writes its config/context/memory under $HOME, and a
# runtime that hands the container HOME=/root (kubelet has been known to) would
# turn the first session into "mkdir /root/.cfassist: permission denied".
ENV HOME=/home/worker

ENTRYPOINT ["/usr/local/bin/cockpit-entrypoint"]
