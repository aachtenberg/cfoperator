# Build stage
FROM golang:1.22-alpine AS builder

ARG VERSION=dev

WORKDIR /app

# Copy go module files
COPY go.mod go.sum ./
RUN go mod download

# Copy source
COPY *.go ./

# Build static binary
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w -X main.Version=${VERSION}" -o llm-gateway .

# Final stage - minimal image
FROM scratch

# Copy binary
COPY --from=builder /app/llm-gateway /llm-gateway

# Copy CA certificates for HTTPS
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/

# Metadata
LABEL org.opencontainers.image.title="LLM Gateway"
LABEL org.opencontainers.image.description="OpenAI-compatible LLM proxy with routing and observability"
LABEL org.opencontainers.image.version="0.1.0"

EXPOSE 4000

ENTRYPOINT ["/llm-gateway"]
