// Command coroner-agent watches a Kubernetes cluster for failing workloads,
// assembles the evidence contract for each, and hands it to the brain.
//
// With no brain configured the agent writes each contract as JSON to stdout,
// which is how the contract was developed and is still the way to read exactly
// what a diagnosis will rest on. With --brain-url set, each contract is posted
// to the brain and the verdict is logged; the brain's own output sink is then
// what a human sees. The agent never holds a model credential and the brain
// never holds a cluster credential.
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

	"github.com/saadhtiwana/coroner/agent/internal/brain"
	"github.com/saadhtiwana/coroner/agent/internal/classify"
	"github.com/saadhtiwana/coroner/agent/internal/collect"
	"github.com/saadhtiwana/coroner/agent/internal/contract"
	"github.com/saadhtiwana/coroner/agent/internal/kube"
	"github.com/saadhtiwana/coroner/agent/internal/remediate"
)

var version = "dev"

type options struct {
	kubeconfig   string
	namespace    string
	once         bool
	compact      bool
	logLevel     string
	resync       time.Duration
	showVersion  bool
	brainURL     string
	brainTimeout time.Duration
	emit         bool

	// Remediation. execute defaults to false: the agent emits every plan
	// and applies none until this is set on purpose, and the write RBAC in
	// deploy/manifests/rbac-write.yaml is applied alongside it.
	execute           bool
	approvalSecret    string
	remediateInterval time.Duration
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
	flag.StringVar(&o.brainURL, "brain-url", os.Getenv("CORONER_BRAIN_URL"), "base URL of the brain; empty means write contracts to stdout instead (env CORONER_BRAIN_URL)")
	flag.DurationVar(&o.brainTimeout, "brain-timeout", brain.DefaultTimeout, "timeout for one diagnosis round trip")
	flag.BoolVar(&o.emit, "emit", false, "also write each contract to stdout when a brain is configured")
	flag.BoolVar(&o.execute, "execute", false, "apply approved remediations; off by default, plans are only emitted")
	flag.StringVar(&o.approvalSecret, "approval-secret", os.Getenv("CORONER_APPROVAL_SECRET"), "secret shared with the brain for verifying approval tokens (env CORONER_APPROVAL_SECRET)")
	flag.DurationVar(&o.remediateInterval, "remediate-interval", 30*time.Second, "how often to ask the brain for approved incidents")
	flag.Parse()
	return o
}

func run(opts options, logger *slog.Logger) error {
	client, err := kube.NewClient(kube.Config{KubeconfigPath: opts.kubeconfig})
	if err != nil {
		return fmt.Errorf("building Kubernetes client: %w", err)
	}

	collector := collect.New(client, collect.ClientLogFetcher{Client: client})

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	handler, err := newHandler(ctx, opts, logger)
	if err != nil {
		return err
	}

	logger.Info("coroner-agent starting",
		"version", version,
		"contract_version", contract.Version,
		"namespace", displayNamespace(opts.namespace),
		"mode", modeName(opts.once),
		"output", handler.name(),
	)

	runner := newRunner(client, handler, opts, logger)

	if opts.once {
		if err := scanOnce(ctx, client, collector, handler, opts.namespace, logger); err != nil {
			return err
		}
		if runner != nil {
			if err := runner.RunOnce(ctx); err != nil {
				return fmt.Errorf("remediation pass: %w", err)
			}
		}
		return nil
	}
	if runner != nil {
		go runner.Run(ctx, opts.remediateInterval)
	}
	return watch(ctx, client, collector, handler, opts, logger)
}

// newRunner wires remediation when a brain is configured. Without a brain
// there are no approvals to act on. Without a secret the runner still polls
// and refuses every approval with the reason, so a misconfiguration is
// visible in the log rather than silent.
func newRunner(client kubernetes.Interface, h handler, opts options, logger *slog.Logger) *remediate.Runner {
	bh, ok := h.(*brainHandler)
	if !ok {
		return nil
	}
	if opts.approvalSecret == "" {
		logger.Warn("no approval secret configured; every approval will be refused")
	}
	if opts.execute {
		logger.Warn("execution is ENABLED; approved plans will be applied to the cluster")
	} else {
		logger.Info("execution is disabled; approved plans will be emitted and not applied")
	}
	runner := &remediate.Runner{
		Brain:    bh.client,
		Secret:   []byte(opts.approvalSecret),
		Executor: remediate.Executor{Client: client, Enabled: opts.execute},
		Resolver: remediate.NewResolver(client),
		Logger:   logger,
		Known:    bh.known,
	}
	runner.Tracking = func(ctx context.Context, incidentID string, t remediate.Target) {
		go runner.Track(ctx, incidentID, t)
	}
	bh.remember = runner.Remember
	return runner
}

// handler receives each assembled contract.
type handler interface {
	handle(ctx context.Context, c *contract.Contract) error
	name() string
}

// newHandler picks the destination. stdout is the default and needs nothing;
// the brain is opt-in and is checked at startup so a wrong URL or a brain
// without credentials fails before the first incident, not on it.
func newHandler(ctx context.Context, opts options, logger *slog.Logger) (handler, error) {
	emitter := newEmitter(os.Stdout, opts.compact)
	if opts.brainURL == "" {
		return emitter, nil
	}

	client, err := brain.New(opts.brainURL, opts.brainTimeout)
	if err != nil {
		return nil, fmt.Errorf("configuring brain client: %w", err)
	}
	health, err := client.Health(ctx)
	if err != nil {
		return nil, fmt.Errorf("brain is not reachable: %w", err)
	}
	if health.ContractVersion != contract.Version {
		return nil, fmt.Errorf("brain speaks contract version %q, this agent emits %q", health.ContractVersion, contract.Version)
	}
	if !health.CredentialsPresent {
		logger.Warn("brain reports no model credential; every contract will be refused", "brain", opts.brainURL)
	}
	logger.Info("brain reachable", "brain", opts.brainURL, "brain_version", health.Version, "model", health.Model)

	h := &brainHandler{client: client, logger: logger, known: &sync.Map{}}
	if opts.emit {
		h.also = emitter
	}
	return h, nil
}

// brainHandler posts the contract and logs the verdict. The verdict's text is
// not printed here: the brain's sink renders it with observed and inferred
// facts kept apart, and printing it a second time from the agent would be a
// second rendering to keep consistent.
type brainHandler struct {
	client *brain.Client
	logger *slog.Logger
	also   *emitter

	// known records the context hash the brain returned for each contract
	// this process sent, so an approval can be checked against the evidence
	// the agent actually saw.
	known    *sync.Map
	remember func(incidentID, contextHash string)
}

func (h *brainHandler) name() string { return "brain" }

func (h *brainHandler) handle(ctx context.Context, c *contract.Contract) error {
	if h.also != nil {
		if err := h.also.emit(c); err != nil {
			return fmt.Errorf("emitting contract %s: %w", c.IncidentID, err)
		}
	}
	v, err := h.client.Diagnose(ctx, c)
	if err != nil {
		return fmt.Errorf("diagnosing %s: %w", c.IncidentID, err)
	}
	if h.remember != nil && v.ContextHash != "" {
		h.remember(v.IncidentID, v.ContextHash)
	}
	attrs := []any{
		"incident_id", v.IncidentID,
		"outcome", v.Outcome,
		"evidence_class", v.EvidenceClass,
		"approvable", v.Approvable,
	}
	if v.ConfidenceFinal != nil {
		attrs = append(attrs, "confidence_final", *v.ConfidenceFinal)
	}
	if v.ConfidenceCeiling != nil {
		attrs = append(attrs, "confidence_ceiling", *v.ConfidenceCeiling)
	}
	if v.Abstained {
		attrs = append(attrs, "abstain_reason", v.AbstainReason)
	} else {
		attrs = append(attrs, "root_cause", v.RootCause)
	}
	h.logger.Info("verdict received", attrs...)
	return nil
}

// scanOnce inspects the pods that exist right now. Used to verify emission
// against a cluster whose failures were induced deliberately.
func scanOnce(ctx context.Context, client kubernetes.Interface, collector *collect.Collector, h handler, namespace string, logger *slog.Logger) error {
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
		if err := emitOne(ctx, collector, h, pod, container, result, logger); err != nil {
			logger.Error("incident not delivered", "pod", pod.Namespace+"/"+pod.Name, "error", err)
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
func watch(ctx context.Context, client kubernetes.Interface, collector *collect.Collector, h handler, opts options, logger *slog.Logger) error {
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
		if err := emitOne(ctx, collector, h, pod, container, result, logger); err != nil {
			logger.Error("incident not delivered", "pod", pod.Namespace+"/"+pod.Name, "error", err)
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

func emitOne(ctx context.Context, collector *collect.Collector, h handler, pod *corev1.Pod, container string, result classify.Result, logger *slog.Logger) error {
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
	return h.handle(ctx, c)
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

func (e *emitter) name() string { return "stdout" }

func (e *emitter) handle(_ context.Context, c *contract.Contract) error { return e.emit(c) }

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
