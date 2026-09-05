// Command coroner-agent watches a Kubernetes cluster for failing workloads,
// assembles the evidence contract, and executes approved remediations.
//
// This is a scaffold. Detection and collection are not implemented yet; the
// next milestone is detecting CrashLoopBackOff and printing the assembled
// context to stdout, with no brain and no LLM involved.
package main

import (
	"flag"
	"fmt"
	"log/slog"
	"os"

	"github.com/saadhtiwana/coroner/agent/internal/contract"
	"github.com/saadhtiwana/coroner/agent/internal/kube"
)

// version is overridden at build time via -ldflags.
var version = "dev"

func main() {
	var (
		kubeconfig  = flag.String("kubeconfig", "", "path to kubeconfig; empty means in-cluster, then the default location")
		logLevel    = flag.String("log-level", "info", "one of debug, info, warn, error")
		showVersion = flag.Bool("version", false, "print version and exit")
	)
	flag.Parse()

	if *showVersion {
		fmt.Printf("coroner-agent %s (contract v%s)\n", version, contract.Version)
		return
	}

	logger := newLogger(*logLevel)
	logger.Info("coroner-agent starting",
		"version", version,
		"contract_version", contract.Version,
	)

	client, err := kube.NewClient(kube.Config{KubeconfigPath: *kubeconfig})
	if err != nil {
		logger.Error("could not build a Kubernetes client", "error", err)
		os.Exit(1)
	}
	_ = client

	logger.Info("scaffold only: detection and collection are not implemented yet")
}

func newLogger(level string) *slog.Logger {
	var l slog.Level
	if err := l.UnmarshalText([]byte(level)); err != nil {
		l = slog.LevelInfo
	}
	return slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: l}))
}
