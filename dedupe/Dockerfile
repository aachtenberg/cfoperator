# cfoperator-dedupe: the remediation dedupe decision (CFOP-509). A stateless
# HTTP service behind the cfop-dedupe Service: the agent sends one candidate
# row with the open rows and the known lists, and applies the answer itself.
# No database, no Kubernetes access, no outbound network.
#
# Multi-arch (amd64 + arm64) with no QEMU: Go cross-compiles on the build
# platform and the final stage only copies the static binary.

# Global, so the FROM below can read it (an ARG after the first FROM is
# scoped to its stage and does nothing for a base image).
ARG GO_IMAGE=golang:1.25-bookworm

FROM --platform=$BUILDPLATFORM ${GO_IMAGE} AS build
ARG TARGETARCH
WORKDIR /src
COPY go.mod ./
COPY *.go ./
COPY cmd/ ./cmd/
RUN CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH} \
    go build -trimpath -ldflags "-s -w" -o /out/cfop-dedupe ./cmd/cfop-dedupe

FROM scratch
COPY --from=build /out/cfop-dedupe /cfop-dedupe
USER 10001:10001
ENV CFOP_DEDUPE_HOST=0.0.0.0
ENV CFOP_DEDUPE_PORT=8093
EXPOSE 8093
ENTRYPOINT ["/cfop-dedupe"]
