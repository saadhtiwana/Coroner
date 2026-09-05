package collect

import (
	"context"
	"fmt"
	"io"
	"strings"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/client-go/kubernetes"
)

// LogFetcher retrieves container logs.
//
// It is an interface because log retrieval is the one part of collection that
// cannot be exercised against a fake clientset: the fake returns a fixed
// string rather than replaying recorded output. Injecting it lets the offline
// tests replay the real recordings, including the recorded failure where the
// runtime had already reclaimed the container.
type LogFetcher interface {
	Fetch(ctx context.Context, namespace, pod, container string, previous bool, tailLines int64) (string, error)
}

// ClientLogFetcher reads logs through the Kubernetes API.
type ClientLogFetcher struct {
	Client kubernetes.Interface
}

// Fetch streams the container log.
//
// An error here is meaningful rather than incidental: it usually means the
// container has been reclaimed, which is a fact the contract records as
// logs_available=false rather than an absence to paper over.
func (f ClientLogFetcher) Fetch(ctx context.Context, namespace, pod, container string, previous bool, tailLines int64) (string, error) {
	opts := &corev1.PodLogOptions{
		Container: container,
		Previous:  previous,
	}
	if tailLines > 0 {
		opts.TailLines = &tailLines
	}

	stream, err := f.Client.CoreV1().Pods(namespace).GetLogs(pod, opts).Stream(ctx)
	if err != nil {
		return "", fmt.Errorf("streaming logs for %s/%s container %s (previous=%t): %w", namespace, pod, container, previous, err)
	}
	defer func() { _ = stream.Close() }()

	var b strings.Builder
	if _, err := io.Copy(&b, stream); err != nil {
		return "", fmt.Errorf("reading log stream for %s/%s container %s: %w", namespace, pod, container, err)
	}
	return b.String(), nil
}

// runtimeFailureBodies are responses the kubelet returns with HTTP 200 whose
// body is the failure notice rather than log content.
//
// Observed against a live cluster: a container reclaimed by containerd yields
// "unable to retrieve container logs for containerd://<id>" as the entire
// body. Treating that as a log makes logs_available true for a contract that
// carries no logs, which defeats the distinction the confidence ceilings
// depend on, and the text itself trips fatal-line detection.
var runtimeFailureBodies = []string{
	"unable to retrieve container logs for",
	"previous terminated container",
	"is waiting to start",
	"is not available",
}

// isRuntimeFailureBody reports whether a log body is really a retrieval
// failure. The match is deliberately narrow: a single short line matching a
// known kubelet phrase, so an application that happens to log similar words
// across many lines is not mistaken for one.
func isRuntimeFailureBody(body string) bool {
	trimmed := strings.TrimSpace(body)
	if trimmed == "" || strings.Count(trimmed, "\n") > 0 {
		return false
	}
	lower := strings.ToLower(trimmed)
	for _, phrase := range runtimeFailureBodies {
		if strings.HasPrefix(lower, phrase) || strings.Contains(lower, phrase) {
			return true
		}
	}
	return false
}

// tailBytes truncates from the front, keeping the end of the log.
//
// The fatal line is at the end. Truncating the head loses startup context;
// truncating the tail loses the cause.
func tailBytes(s string, max int) (string, bool) {
	if max <= 0 || len(s) <= max {
		return s, false
	}
	cut := s[len(s)-max:]
	// Drop the partial first line so the output does not begin mid-token.
	if i := strings.IndexByte(cut, '\n'); i >= 0 && i+1 < len(cut) {
		cut = cut[i+1:]
	}
	return cut, true
}
