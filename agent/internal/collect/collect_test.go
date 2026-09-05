package collect

import (
	"context"
	"strings"
	"testing"

	"github.com/saadhtiwana/coroner/agent/internal/classify"
)

// TestFixturesClassifyAndCollect runs the real collector against every
// recorded incident with no cluster present.
func TestFixturesClassifyAndCollect(t *testing.T) {
	cases := []struct {
		fixture      string
		wantType     classify.FailureType
		wantRule     string
		wantExitCode int32
		wantLogs     bool
		wantFromPrev bool
	}{
		{
			fixture:      "crashloopbackoff",
			wantType:     classify.CrashLoopBackOff,
			wantRule:     "nonzero-exit-with-backoff",
			wantExitCode: 1,
			wantLogs:     true,
			wantFromPrev: true,
		},
		{
			fixture:  "imagepullbackoff",
			wantType: classify.ImagePullBackOff,
			wantRule: "waiting-reason-is-image-pull",
			wantLogs: false,
		},
		{
			// The previous container was reclaimed, but the current container's
			// logs survive and show the memory ramp, so the fallback yields a
			// body. FromPrev is false, which is itself part of the evidence.
			fixture:      "oomkilled",
			wantType:     classify.OOMKilled,
			wantRule:     "terminated-reason-is-oomkilled",
			wantExitCode: 137,
			wantLogs:     true,
			wantFromPrev: false,
		},
		{
			fixture:      "oom-startError",
			wantType:     classify.OOMKilledDuringInit,
			wantRule:     "terminated-message-names-oom",
			wantExitCode: 128,
			wantLogs:     false,
		},
	}

	for _, tc := range cases {
		t.Run(tc.fixture, func(t *testing.T) {
			c, pod, _ := loadFixture(t, tc.fixture)

			result, container := classify.Pod(pod)
			if result.Type != tc.wantType {
				t.Fatalf("classified as %q via rule %q, want %q", result.Type, result.Rule, tc.wantType)
			}
			if result.Rule != tc.wantRule {
				t.Errorf("rule = %q, want %q", result.Rule, tc.wantRule)
			}

			got, err := c.Collect(context.Background(), pod, container, result)
			if err != nil {
				t.Fatalf("Collect() error: %v", err)
			}

			if got.FailureType != string(tc.wantType) {
				t.Errorf("FailureType = %q, want %q", got.FailureType, tc.wantType)
			}
			if got.Pod.UID != string(pod.UID) {
				t.Errorf("Pod.UID = %q, want %q", got.Pod.UID, pod.UID)
			}
			if got.ContractVersion == "" || got.IncidentID == "" {
				t.Error("ContractVersion and IncidentID must both be set")
			}
			if tc.wantExitCode != 0 {
				if got.Container.LastTerminated == nil {
					t.Fatal("LastTerminated is nil, want a termination record")
				}
				if got.Container.LastTerminated.ExitCode != tc.wantExitCode {
					t.Errorf("exit code = %d, want %d", got.Container.LastTerminated.ExitCode, tc.wantExitCode)
				}
			}
			if got.Logs.Available != tc.wantLogs {
				t.Errorf("Logs.Available = %t, want %t", got.Logs.Available, tc.wantLogs)
			}
			if tc.wantLogs && got.Logs.FromPrev != tc.wantFromPrev {
				t.Errorf("Logs.FromPrev = %t, want %t", got.Logs.FromPrev, tc.wantFromPrev)
			}
			if got.Node.Name != pod.Spec.NodeName {
				t.Errorf("Node.Name = %q, want %q", got.Node.Name, pod.Spec.NodeName)
			}
		})
	}
}

// TestEventsAreFilteredByUID is the regression test for the recorded trap:
// some fixtures contain events belonging to more than one pod incarnation of
// the same name, and filtering by name attributes the wrong failures.
//
// The fake clientset ignores field selectors, so every seeded event reaches
// the collector and the local UID filter is what has to do the work.
func TestEventsAreFilteredByUID(t *testing.T) {
	for _, fixture := range []string{"crashloopbackoff", "imagepullbackoff", "oomkilled", "oom-startError"} {
		t.Run(fixture, func(t *testing.T) {
			c, pod, seeded := loadFixture(t, fixture)
			result, container := classify.Pod(pod)

			got, err := c.Collect(context.Background(), pod, container, result)
			if err != nil {
				t.Fatalf("Collect() error: %v", err)
			}
			if len(got.Events) == 0 {
				t.Fatal("no events collected")
			}

			distinct := map[string]bool{}
			matching := 0
			for i := range seeded {
				uid := string(seeded[i].InvolvedObject.UID)
				distinct[uid] = true
				if uid == string(pod.UID) {
					matching++
				}
			}

			if len(got.Events) != matching {
				t.Errorf("collected %d events, want %d belonging to uid %s (of %d seeded across %d incarnations)",
					len(got.Events), matching, pod.UID, len(seeded), len(distinct))
			}

			if len(distinct) > 1 && len(got.Events) >= len(seeded) {
				t.Errorf("recording holds %d incarnations but all %d events survived; UID filtering did not happen",
					len(distinct), len(got.Events))
			}

			t.Logf("kept %d of %d seeded events across %d incarnations", len(got.Events), len(seeded), len(distinct))
		})
	}
}

// TestAggregationIsReconstructed confirms the "x5 over 2m42s" phrasing that a
// naive field selection would have discarded.
func TestAggregationIsReconstructed(t *testing.T) {
	c, pod, _ := loadFixture(t, "crashloopbackoff")
	result, container := classify.Pod(pod)
	got, err := c.Collect(context.Background(), pod, container, result)
	if err != nil {
		t.Fatalf("Collect() error: %v", err)
	}

	var found bool
	for _, e := range got.Events {
		if e.Occurrences > 1 {
			if e.Aggregated == "" {
				t.Errorf("event %s/%s has %d occurrences but no aggregated phrasing", e.Type, e.Reason, e.Occurrences)
			}
			if !strings.HasPrefix(e.Aggregated, "x") {
				t.Errorf("aggregated = %q, want it to start with x", e.Aggregated)
			}
			found = true
		}
	}
	if !found {
		t.Error("no repeated events found; the flap signal is not being preserved")
	}
}

// TestCausalLineSurvivesRedaction guards the single most important property of
// the CrashLoopBackOff contract: the one line that names the actual cause must
// reach the brain intact.
func TestCausalLineSurvivesRedaction(t *testing.T) {
	c, pod, _ := loadFixture(t, "crashloopbackoff")
	result, container := classify.Pod(pod)
	got, err := c.Collect(context.Background(), pod, container, result)
	if err != nil {
		t.Fatalf("Collect() error: %v", err)
	}

	if !got.Logs.Available || got.Logs.Empty {
		t.Fatalf("logs missing: available=%t empty=%t", got.Logs.Available, got.Logs.Empty)
	}
	if !got.Logs.FromPrev {
		t.Error("logs should have come from the previous container")
	}
	for _, want := range []string{
		"connection refused",
		"could not initialise connection pool",
		"postgres://orders@db.internal:5432/orders",
	} {
		if !strings.Contains(got.Logs.Content, want) {
			t.Errorf("log content lost %q\ngot:\n%s", want, got.Logs.Content)
		}
	}
	if got.RedactedCount != 0 {
		t.Errorf("redacted %d item(s) from a log with no secrets: %v", got.RedactedCount, got.RedactedKinds)
	}
}

// TestEnvValuesAreNeverCollected checks the structural guarantee rather than
// the redactor: values are not read at all.
func TestEnvValuesAreNeverCollected(t *testing.T) {
	c, pod, _ := loadFixture(t, "crashloopbackoff")
	result, container := classify.Pod(pod)
	got, err := c.Collect(context.Background(), pod, container, result)
	if err != nil {
		t.Fatalf("Collect() error: %v", err)
	}

	for i := range pod.Spec.Containers {
		for _, env := range pod.Spec.Containers[i].Env {
			if env.Value == "" {
				continue
			}
			blob := got.Container.EnvNames
			for _, n := range blob {
				if n == env.Value {
					t.Errorf("env value %q leaked into EnvNames", env.Value)
				}
			}
		}
	}
}

// TestLogsUnavailableIsDistinctFromEmpty covers the OOM case, where the
// runtime reclaimed the container and no log body exists at all.
func TestLogsUnavailableIsDistinctFromEmpty(t *testing.T) {
	c, pod, _ := loadFixture(t, "oom-startError")
	result, container := classify.Pod(pod)
	got, err := c.Collect(context.Background(), pod, container, result)
	if err != nil {
		t.Fatalf("Collect() error: %v", err)
	}
	if got.Logs.Available {
		t.Error("Logs.Available = true, want false for a reclaimed container")
	}
	if !got.Logs.Empty {
		t.Error("Logs.Empty = false, want true when nothing was retrieved")
	}
	if got.Logs.Content != "" {
		t.Errorf("Logs.Content = %q, want empty", got.Logs.Content)
	}
}

// TestDerivedFieldsComputedInAgent confirms the rate is calculated here rather
// than left for a model to derive from timestamps.
func TestDerivedFieldsComputedInAgent(t *testing.T) {
	c, pod, _ := loadFixture(t, "crashloopbackoff")
	result, container := classify.Pod(pod)
	got, err := c.Collect(context.Background(), pod, container, result)
	if err != nil {
		t.Fatalf("Collect() error: %v", err)
	}
	if got.Pod.AgeSecs <= 0 {
		t.Errorf("AgeSecs = %v, want positive", got.Pod.AgeSecs)
	}
	if got.Container.RestartCount > 0 && got.Container.CrashesPerMinute <= 0 {
		t.Errorf("CrashesPerMinute = %v with %d restarts, want positive",
			got.Container.CrashesPerMinute, got.Container.RestartCount)
	}
}

// A body that is really the kubelet's retrieval failure must not be recorded
// as a log. Observed live: a reclaimed container returns HTTP 200 whose entire
// body is "unable to retrieve container logs for containerd://<id>", which
// would otherwise set logs_available true for a contract carrying no logs and
// trip fatal-line detection on the word "unable".
func TestRuntimeFailureBodyIsNotTreatedAsLogs(t *testing.T) {
	cases := []struct {
		name string
		body string
		want bool
	}{
		{"reclaimed container", "unable to retrieve container logs for containerd://8e53e139841", true},
		{"previous container gone", "previous terminated container \"app\" in pod \"x\" not found", true},
		{"waiting to start", "container \"app\" in pod \"x\" is waiting to start: ContainerCreating", true},
		{"real single-line log", "[fatal] could not initialise connection pool", false},
		{"real multi-line log", "[startup] booting\n[error] unable to reach db\n", false},
		{"empty", "", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := isRuntimeFailureBody(tc.body); got != tc.want {
				t.Errorf("isRuntimeFailureBody(%q) = %t, want %t", tc.body, got, tc.want)
			}
		})
	}
}
