// Command coroner-agent watches a Kubernetes cluster for failing workloads and
// emits the assembled evidence contract as JSON.
//
// This milestone stops at emission. There is no brain, no model, no Slack, and
// no HTTP client of any kind: the agent reads the cluster and writes JSON to
// stdout. Everything a diagnosis will later rest on is produced here, so it is
// worth being able to read it directly before anything consumes it.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/informers"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/cache"

	"github.com/saadhtiwana/coroner/agent/internal/classify"
	"github.com/saadhtiwana/coroner/agent/internal/collect"
	"github.com/saadhtiwana/coroner/agent/internal/contract"
	"github.com/saadhtiwana/coroner/agent/internal/kube"
)

var version = "dev"

type options struct {
	kubeconfig  string
	namespace   string
	once        bool
	compact     bool
	logLevel    string
	resync      time.Duration
	showVersion bool
}

func main() {
	opts := parseFlags()

	if opts.showVersion {
		fmt.Printf("coroner-agent %s (contract v%s)\n", version, contract.Version)
		return
	}

	logger := newLogger(opts.logLevel)

	if err := run(opts, logger); err != nil {
		logger.Error("agent failed", "error", err)
		os.Exit(1)
	}
}

func parseFlags() options {
	var o options
	flag.StringVar(&o.kubeconfig, "kubeconfig", "", "path to kubeconfig; empty means in-cluster, then the default location")
	flag.StringVar(&o.namespace, "namespace", metav1.NamespaceAll, "namespace to watch; empty means all namespaces")
	flag.BoolVar(&o.once, "once", false, "scan current pods, emit contracts for those already failing, and exit")
	flag.BoolVar(&o.compact, "compact", false, "emit one contract per line instead of indented JSON")
	flag.StringVar(&o.logLevel, "log-level", "info", "one of debug, info, warn, error")
	flag.DurationVar(&o.resync, "resync", 10*time.Minute, "informer resync period in watch mode")
	flag.BoolVar(&o.showVersion, "version", false, "print version and exit")
	flag.Parse()
	return o
}

func run(opts options, logger *slog.Logger) error {
	client, err := kube.NewClient(kube.Config{KubeconfigPath: opts.kubeconfig})
	if err != nil {
		return fmt.Errorf("building Kubernetes client: %w", err)
	}

	collector := collect.New(client, collect.ClientLogFetcher{Client: client})
	emitter := newEmitter(os.Stdout, opts.compact)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	logger.Info("coroner-agent starting",
		"version", version,
		"contract_version", contract.Version,
		"namespace", displayNamespace(opts.namespace),
		"mode", modeName(opts.once),
	)

	if opts.once {
		return scanOnce(ctx, client, collector, emitter, opts.namespace, logger)
	}
	return watch(ctx, client, collector, emitter, opts, logger)
}

// scanOnce inspects the pods that exist right now. Used to verify emission
// against a cluster whose failures were induced deliberately.
func scanOnce(ctx context.Context, client kubernetes.Interface, collector *collect.Collector, e *emitter, namespace string, logger *slog.Logger) error {
	pods, err := client.CoreV1().Pods(namespace).List(ctx, metav1.ListOptions{})
	if err != nil {
		return fmt.Errorf("listing pods in %s: %w", displayNamespace(namespace), err)
	}

	emitted := 0
	for i := range pods.Items {
		pod := &pods.Items[i]
		result, container := classify.Pod(pod)
		if result.Type == classify.None {
			continue
		}
		if err := emitOne(ctx, collector, e, pod, container, result, logger); err != nil {
			logger.Error("collection failed", "pod", pod.Namespace+"/"+pod.Name, "error", err)
			continue
		}
		emitted++
	}

	logger.Info("scan complete", "pods_examined", len(pods.Items), "contracts_emitted", emitted)
	return nil
}

// watch emits a contract each time a pod enters a distinct failure occurrence.
//
// Deduplication is by incident id, which is derived from pod uid, container,
// restart count and failure type. A pod that restarts again is a new
// occurrence and is emitted again; the same occurrence observed twice is not.
func watch(ctx context.Context, client kubernetes.Interface, collector *collect.Collector, e *emitter, opts options, logger *slog.Logger) error {
	factory := informers.NewSharedInformerFactoryWithOptions(client, opts.resync, informers.WithNamespace(opts.namespace))
	podInformer := factory.Core().V1().Pods().Informer()

	seen := newSeenSet()

	handle := func(obj any) {
		pod, ok := obj.(*corev1.Pod)
		if !ok {
			return
		}
		result, container := classify.Pod(pod)
		if result.Type == classify.None {
			return
		}
		id := collect.IncidentID(string(pod.UID), container, restartCount(pod, container), string(result.Type))
		if !seen.markNew(id) {
			return
		}
		if err := emitOne(ctx, collector, e, pod, container, result, logger); err != nil {
			logger.Error("collection failed", "pod", pod.Namespace+"/"+pod.Name, "error", err)
		}
	}

	if _, err := podInformer.AddEventHandler(cache.ResourceEventHandlerFuncs{
		AddFunc:    handle,
		UpdateFunc: func(_, newObj any) { handle(newObj) },
	}); err != nil {
		return fmt.Errorf("registering pod handler: %w", err)
	}

	factory.Start(ctx.Done())
	for informerType, ok := range factory.WaitForCacheSync(ctx.Done()) {
		if !ok {
			return fmt.Errorf("cache for %v failed to sync", informerType)
		}
	}
	logger.Info("watching for failing pods")

	<-ctx.Done()
	logger.Info("shutting down")
	return nil
}

func emitOne(ctx context.Context, collector *collect.Collector, e *emitter, pod *corev1.Pod, container string, result classify.Result, logger *slog.Logger) error {
	c, err := collector.Collect(ctx, pod, container, result)
	if err != nil {
		return fmt.Errorf("collecting contract for %s/%s container %s: %w", pod.Namespace, pod.Name, container, err)
	}
	logger.Info("failure detected",
		"pod", pod.Namespace+"/"+pod.Name,
		"container", container,
		"failure_type", c.FailureType,
		"rule", result.Rule,
		"incident_id", c.IncidentID,
		"logs_available", c.Logs.Available,
	)
	return e.emit(c)
}

func restartCount(pod *corev1.Pod, container string) int32 {
	for i := range pod.Status.ContainerStatuses {
		if pod.Status.ContainerStatuses[i].Name == container {
			return pod.Status.ContainerStatuses[i].RestartCount
		}
	}
	for i := range pod.Status.InitContainerStatuses {
		if pod.Status.InitContainerStatuses[i].Name == container {
			return pod.Status.InitContainerStatuses[i].RestartCount
		}
	}
	return 0
}

// emitter serialises contracts to a writer. Writes are serialised because the
// informer may deliver from more than one goroutine and interleaved JSON is
// unparseable.
type emitter struct {
	mu  sync.Mutex
	enc *json.Encoder
}

func newEmitter(w *os.File, compact bool) *emitter {
	enc := json.NewEncoder(w)
	if !compact {
		enc.SetIndent("", "  ")
	}
	return &emitter{enc: enc}
}

func (e *emitter) emit(c *contract.Contract) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	if err := e.enc.Encode(c); err != nil {
		return fmt.Errorf("encoding contract %s: %w", c.IncidentID, err)
	}
	return nil
}

type seenSet struct {
	mu  sync.Mutex
	ids map[string]struct{}
}

func newSeenSet() *seenSet {
	return &seenSet{ids: make(map[string]struct{})}
}

// markNew records an id and reports whether it had not been seen before.
func (s *seenSet) markNew(id string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.ids[id]; ok {
		return false
	}
	s.ids[id] = struct{}{}
	return true
}

func displayNamespace(ns string) string {
	if ns == metav1.NamespaceAll {
		return "(all)"
	}
	return ns
}

func modeName(once bool) string {
	if once {
		return "once"
	}
	return "watch"
}

func newLogger(level string) *slog.Logger {
	var l slog.Level
	if err := l.UnmarshalText([]byte(level)); err != nil {
		l = slog.LevelInfo
	}
	// Logs go to stderr so stdout carries only contract JSON.
	return slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: l}))
}
