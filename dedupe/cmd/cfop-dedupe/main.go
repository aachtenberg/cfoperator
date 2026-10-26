// Command cfop-dedupe serves the dedupe decision (CFOP-509) over HTTP.
//
// Environment:
//
//	CFOP_DEDUPE_HOST           listen address (default 0.0.0.0)
//	CFOP_DEDUPE_PORT           listen port (default 8093)
//	CFOP_DEDUPE_SHARED_SECRET  X-CFOP-Token value; unset accepts every request
package main

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/aachtenberg/cfoperator/dedupe"
)

func main() {
	log := slog.New(slog.NewTextHandler(os.Stdout, nil))
	addr := net.JoinHostPort(env("CFOP_DEDUPE_HOST", "0.0.0.0"), env("CFOP_DEDUPE_PORT", "8093"))
	secret := strings.TrimSpace(os.Getenv("CFOP_DEDUPE_SHARED_SECRET"))
	if secret == "" {
		log.Warn("CFOP_DEDUPE_SHARED_SECRET is unset; accepting unauthenticated requests")
	}
	srv := &http.Server{
		Addr:              addr,
		Handler:           dedupe.Handler(secret, log),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      30 * time.Second,
	}

	// SIGTERM drains in-flight decisions, so a rolling update never cuts one
	// off. The agent falls back to its own exact-key check if one is lost.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, os.Interrupt)
	defer stop()
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdown)
	}()

	log.Info("cfop-dedupe listening", "addr", addr)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Error("server failed", "err", err)
		os.Exit(1)
	}
}

func env(name, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(name)); v != "" {
		return v
	}
	return fallback
}
