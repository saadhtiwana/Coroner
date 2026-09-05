package collect

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes/fake"
)

// fixtureRoot is the recorded evidence checked into the repository.
func fixtureRoot(t *testing.T) string {
	t.Helper()
	root, err := filepath.Abs(filepath.Join("..", "..", "..", "fixtures"))
	if err != nil {
		t.Fatalf("resolving fixtures path: %v", err)
	}
	if _, err := os.Stat(root); err != nil {
		t.Fatalf("fixtures not found at %s: %v", root, err)
	}
	return root
}

func readJSON(t *testing.T, path string, into any) {
	t.Helper()
	b, err := os.ReadFile(path) //nolint:gosec // test-only path under the repo
	if err != nil {
		t.Fatalf("reading %s: %v", path, err)
	}
	if err := json.Unmarshal(b, into); err != nil {
		t.Fatalf("parsing %s: %v", path, err)
	}
}

// fixtureLogFetcher replays recorded log output.
//
// kubectl writes its retrieval failure into the captured file, so a recording
// that begins with that text is replayed as an error, which is what the API
// returns in the same situation.
type fixtureLogFetcher struct {
	prev    string
	current string
}

func (f fixtureLogFetcher) Fetch(_ context.Context, _, _, _ string, previous bool, _ int64) (string, error) {
	body := f.current
	if previous {
		body = f.prev
	}
	if isRetrievalFailure(body) {
		return "", fmt.Errorf("unable to retrieve container logs")
	}
	return body, nil
}

func isRetrievalFailure(s string) bool {
	trimmed := strings.TrimSpace(s)
	return trimmed == "" ||
		strings.HasPrefix(trimmed, "unable to retrieve container logs") ||
		strings.HasPrefix(trimmed, "(no previous logs available)") ||
		strings.HasPrefix(trimmed, "(no current logs available)") ||
		strings.Contains(trimmed, "previous terminated container") ||
		strings.Contains(trimmed, "not found")
}

func readText(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path) //nolint:gosec // test-only path under the repo
	if err != nil {
		return ""
	}
	return string(b)
}

// loadFixture builds a fake cluster from one recorded incident.
//
// Every event in the recording is seeded, including those belonging to other
// pod incarnations. The fake clientset ignores field selectors, so the
// collector's local UID filter is what has to produce the right answer, which
// is exactly the behaviour under test.
func loadFixture(t *testing.T, name string) (*Collector, *corev1.Pod, []corev1.Event) {
	t.Helper()
	root := fixtureRoot(t)
	dir := filepath.Join(root, name)

	pod := &corev1.Pod{}
	readJSON(t, filepath.Join(dir, "pod.json"), pod)

	events := &corev1.EventList{}
	readJSON(t, filepath.Join(dir, "events.json"), events)

	nodes := &corev1.NodeList{}
	readJSON(t, filepath.Join(root, "nodes.json"), nodes)

	objects := []runtime.Object{pod}
	for i := range events.Items {
		objects = append(objects, &events.Items[i])
	}
	for i := range nodes.Items {
		objects = append(objects, &nodes.Items[i])
	}

	client := fake.NewSimpleClientset(objects...)
	c := New(client, fixtureLogFetcher{
		prev:    readText(t, filepath.Join(dir, "logs-previous.txt")),
		current: readText(t, filepath.Join(dir, "logs-current.txt")),
	})
	// Fixed clock so age and rate are deterministic. Chosen to sit after every
	// recorded timestamp.
	c.Now = func() time.Time { return pod.CreationTimestamp.Add(162 * time.Second) }

	return c, pod, events.Items
}
