package classify

import (
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func waiting(reason, msg string) corev1.ContainerState {
	return corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{Reason: reason, Message: msg}}
}

func terminated(reason string, exit int32, msg string) corev1.ContainerState {
	return corev1.ContainerState{Terminated: &corev1.ContainerStateTerminated{
		Reason: reason, ExitCode: exit, Message: msg,
	}}
}

func TestClassifyContainer(t *testing.T) {
	cases := []struct {
		name     string
		status   corev1.ContainerStatus
		wantType FailureType
		wantRule string
	}{
		{
			name: "image pull backoff",
			status: corev1.ContainerStatus{
				State: waiting("ImagePullBackOff", `Back-off pulling image "ghcr.io/x/y:v0"`),
			},
			wantType: ImagePullBackOff,
			wantRule: "waiting-reason-is-image-pull",
		},
		{
			name: "err image pull is the same problem",
			status: corev1.ContainerStatus{
				State: waiting("ErrImagePull", "403 Forbidden"),
			},
			wantType: ImagePullBackOff,
			wantRule: "waiting-reason-is-image-pull",
		},
		{
			name: "oom killed while running",
			status: corev1.ContainerStatus{
				State:                waiting("CrashLoopBackOff", "back-off restarting"),
				LastTerminationState: terminated("OOMKilled", 137, ""),
			},
			wantType: OOMKilled,
			wantRule: "terminated-reason-is-oomkilled",
		},
		{
			name: "oom killed during container init",
			status: corev1.ContainerStatus{
				State: waiting("CrashLoopBackOff", "back-off restarting"),
				LastTerminationState: terminated("StartError", 128,
					"failed to create containerd task: ... container init was OOM-killed (memory limit too low?)"),
			},
			wantType: OOMKilledDuringInit,
			wantRule: "terminated-message-names-oom",
		},
		{
			name: "ordinary application crash",
			status: corev1.ContainerStatus{
				State:                waiting("CrashLoopBackOff", "back-off restarting"),
				LastTerminationState: terminated("Error", 1, ""),
			},
			wantType: CrashLoopBackOff,
			wantRule: "nonzero-exit-with-backoff",
		},
		{
			name: "crashloop with no termination detail retained",
			status: corev1.ContainerStatus{
				State: waiting("CrashLoopBackOff", "back-off restarting"),
			},
			wantType: CrashLoopBackOff,
			wantRule: "waiting-reason-is-crashloop-without-termination-detail",
		},
		{
			name:     "healthy running container",
			status:   corev1.ContainerStatus{State: corev1.ContainerState{Running: &corev1.ContainerStateRunning{}}, Ready: true},
			wantType: None,
		},
		{
			name: "clean exit is not a failure",
			status: corev1.ContainerStatus{
				LastTerminationState: terminated("Completed", 0, ""),
			},
			wantType: None,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := Container(&tc.status)
			if got.Type != tc.wantType {
				t.Errorf("Type = %q (rule %q), want %q", got.Type, got.Rule, tc.wantType)
			}
			if tc.wantRule != "" && got.Rule != tc.wantRule {
				t.Errorf("Rule = %q, want %q", got.Rule, tc.wantRule)
			}
		})
	}
}

// The 64Mi fixture exists to prove this case. A classifier keyed on exit code
// 137 misses a real memory kill, and one keyed on the surface waiting reason
// files it as an ordinary crash.
func TestStartErrorOOMIsNotMisfiledAsCrashLoop(t *testing.T) {
	status := corev1.ContainerStatus{
		State: waiting("CrashLoopBackOff", "back-off 1m20s restarting failed container"),
		LastTerminationState: terminated("StartError", 128,
			"failed to create containerd task: failed to create shim task: OCI runtime create failed: "+
				"runc create failed: unable to start container process: container init was OOM-killed (memory limit too low?)"),
	}

	got := Container(&status)
	if got.Type == CrashLoopBackOff {
		t.Fatal("classified as CrashLoopBackOff; the surface reason was trusted over the termination detail")
	}
	if got.Type != OOMKilledDuringInit {
		t.Fatalf("Type = %q, want %q", got.Type, OOMKilledDuringInit)
	}
	if got.Signals["lastState.terminated.exitCode"] != "128" {
		t.Errorf("exit code signal = %q, want 128", got.Signals["lastState.terminated.exitCode"])
	}
}

// The surface reason is CrashLoopBackOff for a memory kill too. Termination
// detail must be read first or the wrong failure type is reported.
func TestTerminationDetailBeatsSurfaceReason(t *testing.T) {
	status := corev1.ContainerStatus{
		State:                waiting("CrashLoopBackOff", "back-off restarting"),
		LastTerminationState: terminated("OOMKilled", 137, ""),
	}
	if got := Container(&status); got.Type != OOMKilled {
		t.Errorf("Type = %q, want %q", got.Type, OOMKilled)
	}
}

// Exit code alone must never decide. A SIGKILL from any source yields 137.
func TestExitCodeIsCorroborationNotDiscriminator(t *testing.T) {
	t.Run("137 with an informative reason uses the reason", func(t *testing.T) {
		status := corev1.ContainerStatus{
			State:                waiting("CrashLoopBackOff", ""),
			LastTerminationState: terminated("OOMKilled", 137, ""),
		}
		got := Container(&status)
		if got.Rule != "terminated-reason-is-oomkilled" {
			t.Errorf("Rule = %q, want the reason-based rule", got.Rule)
		}
	})

	t.Run("137 with a generic reason falls back to the exit code last", func(t *testing.T) {
		status := corev1.ContainerStatus{
			State:                waiting("CrashLoopBackOff", ""),
			LastTerminationState: terminated("Error", 137, ""),
		}
		got := Container(&status)
		if got.Type != OOMKilled {
			t.Errorf("Type = %q, want %q", got.Type, OOMKilled)
		}
		if got.Rule != "terminated-exitcode-137-corroborated" {
			t.Errorf("Rule = %q, want the corroboration rule", got.Rule)
		}
		if got.Signals["note"] == "" {
			t.Error("corroborated classification should record why it was reached")
		}
	})

	t.Run("128 without an oom marker is not an oom", func(t *testing.T) {
		status := corev1.ContainerStatus{
			State:                waiting("CrashLoopBackOff", ""),
			LastTerminationState: terminated("StartError", 128, "exec: \"/app/server\": stat /app/server: no such file or directory"),
		}
		got := Container(&status)
		if got.Type == OOMKilled || got.Type == OOMKilledDuringInit {
			t.Errorf("Type = %q; exit 128 alone must not imply memory", got.Type)
		}
	})
}

func TestPodPrefersInitContainers(t *testing.T) {
	pod := &corev1.Pod{
		Status: corev1.PodStatus{
			InitContainerStatuses: []corev1.ContainerStatus{
				{Name: "migrate", State: waiting("CrashLoopBackOff", ""), LastTerminationState: terminated("Error", 1, "")},
			},
			ContainerStatuses: []corev1.ContainerStatus{
				{Name: "app", State: corev1.ContainerState{Running: &corev1.ContainerStateRunning{}}},
			},
		},
	}
	got, container := Pod(pod)
	if container != "migrate" {
		t.Errorf("container = %q, want migrate", container)
	}
	if got.Type != CrashLoopBackOff {
		t.Errorf("Type = %q, want %q", got.Type, CrashLoopBackOff)
	}
}

// A container that OOM-killed once and then started successfully is not a live
// incident. Without this guard the stale lastState would be reported forever.
func TestRecoveredContainerIsNotAFailure(t *testing.T) {
	now := time.Date(2026, 9, 5, 12, 0, 0, 0, time.UTC)
	status := corev1.ContainerStatus{
		Ready: true,
		State: corev1.ContainerState{Running: &corev1.ContainerStateRunning{
			StartedAt: metav1.NewTime(now.Add(-10 * time.Minute)),
		}},
		LastTerminationState: terminated("StartError", 128, "container init was OOM-killed (memory limit too low?)"),
	}
	got := ContainerAt(&status, now)
	if got.Type != None {
		t.Errorf("Type = %q via rule %q, want None for a recovered container", got.Type, got.Rule)
	}
	if got.Rule != "container-running-and-stable" {
		t.Errorf("Rule = %q, want container-running-and-stable", got.Rule)
	}
}

// The regression this guards: with no readiness probe a crash-looping
// container is Running and Ready for the seconds between restarts. Treating
// readiness as recovery makes detection a race against the poll interval.
func TestBrieflyRunningCrashLoopIsStillDetected(t *testing.T) {
	now := time.Date(2026, 9, 5, 12, 0, 0, 0, time.UTC)
	status := corev1.ContainerStatus{
		Ready:        true,
		RestartCount: 6,
		State: corev1.ContainerState{Running: &corev1.ContainerStateRunning{
			StartedAt: metav1.NewTime(now.Add(-2 * time.Second)),
		}},
		LastTerminationState: terminated("Error", 1, ""),
	}
	got := ContainerAt(&status, now)
	if got.Type == None {
		t.Fatalf("a container two seconds into a crash cycle was classified as healthy (rule %q)", got.Rule)
	}
	if got.Type != CrashLoopBackOff {
		t.Errorf("Type = %q, want %q", got.Type, CrashLoopBackOff)
	}
}

// Between restarts a crash-looping container is briefly Running and not ready.
// It must still classify, or detection becomes a race with the poll interval.
func TestRunningButNotReadyStillClassifies(t *testing.T) {
	status := corev1.ContainerStatus{
		Ready:                false,
		State:                corev1.ContainerState{Running: &corev1.ContainerStateRunning{}},
		LastTerminationState: terminated("OOMKilled", 137, ""),
	}
	if got := Container(&status); got.Type != OOMKilled {
		t.Errorf("Type = %q, want %q", got.Type, OOMKilled)
	}
}

func TestNilInputs(t *testing.T) {
	if got := Container(nil); got.Type != None {
		t.Errorf("Container(nil) = %q, want None", got.Type)
	}
	if got, _ := Pod(nil); got.Type != None {
		t.Errorf("Pod(nil) = %q, want None", got.Type)
	}
}
