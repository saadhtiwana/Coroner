// Package classify determines which failure shape a container status
// represents.
//
// Two rules govern this package, both taken from recorded evidence in
// fixtures/ rather than from theory.
//
// The surface waiting reason does not identify the failure. CrashLoopBackOff
// appears for a database connection failure, for a memory limit below what
// container init needs, and for an ordinary nonzero exit. Classifying on it
// alone collapses three different problems into one.
//
// The exit code does not identify the failure either. Exit 137 with reason
// OOMKilled and exit 128 with reason StartError are both memory exhaustion,
// and a classifier keyed on 137 misses the second entirely. Exit code is used
// only to corroborate a reason, never as the sole discriminator.
package classify

import (
	"strconv"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
)

// FailureType is a classified failure shape.
type FailureType string

const (
	// None means the container is not in a failure state this agent handles.
	None FailureType = ""

	// CrashLoopBackOff is an application that exits nonzero for its own
	// reasons. The cause is almost never in the API surface.
	CrashLoopBackOff FailureType = "CrashLoopBackOff"

	// OOMKilled is a container killed by the kernel for exceeding its memory
	// limit while running.
	OOMKilled FailureType = "OOMKilled"

	// OOMKilledDuringInit is the same underlying cause as OOMKilled, but the
	// kill lands on the container runtime's init before application code runs.
	// It reports StartError and exit 128 rather than OOMKilled and 137, and
	// surfaces as CrashLoopBackOff or RunContainerError.
	OOMKilledDuringInit FailureType = "OOMKilledDuringInit"

	// ImagePullBackOff covers every failure to obtain the image.
	ImagePullBackOff FailureType = "ImagePullBackOff"
)

// Result is a classification with the reasoning that produced it.
type Result struct {
	Type FailureType

	// Rule names the branch that fired, so a misclassification can be traced
	// to a specific rule rather than guessed at.
	Rule string

	// Signals are the field values the decision rested on. These become the
	// starting evidence set for the diagnosis.
	Signals map[string]string
}

// imagePullWaitingReasons are the kubelet waiting reasons that mean the image
// could not be obtained. ErrImagePull is the immediate failure and
// ImagePullBackOff is the backoff that follows it; both are the same problem.
var imagePullWaitingReasons = map[string]bool{
	"ImagePullBackOff":          true,
	"ErrImagePull":              true,
	"InvalidImageName":          true,
	"ImageInspectError":         true,
	"RegistryUnavailable":       true,
	"SignatureValidationFailed": true,
}

// oomMarkers appear in a runtime message when the kill was for memory. runc
// reports "container init was OOM-killed (memory limit too low?)".
var oomMarkers = []string{
	"oom-killed",
	"oom killed",
	"oomkilled",
	"out of memory",
	"cannot allocate memory",
}

// ExitCodeOOM is the conventional 128+SIGKILL exit for a memory kill. It is
// deliberately not used on its own: a SIGKILL from any source produces it.
const ExitCodeOOM int32 = 137

// StableRunPeriod is how long a running container must have been up before its
// earlier failure is treated as history rather than as a live incident.
//
// Readiness alone cannot make that call. A container with no readiness probe
// is Ready the moment it starts, so a crash-looping workload is Running and
// Ready for the couple of seconds between restarts. Keying recovery on
// readiness would make detection a race against the poll interval.
const StableRunPeriod = 60 * time.Second

// Container classifies a single container status.
//
// Order matters. Terminated state is examined before the waiting reason,
// because the waiting reason is the symptom and the last termination is the
// cause. A container that was OOM-killed and is now backing off reports
// CrashLoopBackOff in state.waiting and OOMKilled in lastState.terminated;
// reading them in the other order yields the wrong answer.
func Container(cs *corev1.ContainerStatus) Result {
	return ContainerAt(cs, time.Now())
}

// ContainerAt is Container with an explicit clock, so the recovery window is
// testable without sleeping.
func ContainerAt(cs *corev1.ContainerStatus, now time.Time) Result {
	if cs == nil {
		return Result{Type: None, Rule: "nil-status"}
	}

	// A container that has been running and ready for a while has recovered.
	// Its lastState still records the earlier failure, and classifying from
	// that would report a healthy workload as a live incident forever.
	if r, ok := classifyRecovered(cs, now); ok {
		return r
	}

	if r, ok := classifyWaitingImagePull(cs); ok {
		return r
	}
	if r, ok := classifyTerminated(cs); ok {
		return r
	}
	if r, ok := classifyWaitingCrashLoop(cs); ok {
		return r
	}
	return Result{Type: None, Rule: "no-rule-matched"}
}

func classifyRecovered(cs *corev1.ContainerStatus, now time.Time) (Result, bool) {
	if cs.State.Running == nil || !cs.Ready {
		return Result{}, false
	}
	started := cs.State.Running.StartedAt.Time
	if started.IsZero() || now.Sub(started) < StableRunPeriod {
		return Result{}, false
	}
	return Result{
		Type: None,
		Rule: "container-running-and-stable",
		Signals: map[string]string{
			"state.running.startedAt": started.UTC().Format(time.RFC3339),
		},
	}, true
}

func classifyWaitingImagePull(cs *corev1.ContainerStatus) (Result, bool) {
	w := cs.State.Waiting
	if w == nil || !imagePullWaitingReasons[w.Reason] {
		return Result{}, false
	}
	return Result{
		Type: ImagePullBackOff,
		Rule: "waiting-reason-is-image-pull",
		Signals: map[string]string{
			"state.waiting.reason":  w.Reason,
			"state.waiting.message": w.Message,
			"image":                 cs.Image,
		},
	}, true
}

func classifyTerminated(cs *corev1.ContainerStatus) (Result, bool) {
	t := cs.LastTerminationState.Terminated
	if t == nil {
		t = cs.State.Terminated
	}
	if t == nil {
		return Result{}, false
	}

	signals := map[string]string{
		"lastState.terminated.reason":   t.Reason,
		"lastState.terminated.exitCode": itoa(t.ExitCode),
	}
	if t.Message != "" {
		signals["lastState.terminated.message"] = t.Message
	}

	// The runtime said so outright.
	if t.Reason == "OOMKilled" {
		return Result{Type: OOMKilled, Rule: "terminated-reason-is-oomkilled", Signals: signals}, true
	}

	// The kill landed on container init. The reason is StartError or
	// ContainerCannotRun and only the message identifies it as memory.
	if hasOOMMarker(t.Message) {
		return Result{Type: OOMKilledDuringInit, Rule: "terminated-message-names-oom", Signals: signals}, true
	}

	// Corroboration only, and last: some runtimes report a memory kill with a
	// generic reason but the conventional exit code. Reached only after both
	// reason and message have failed to identify the failure, so the exit code
	// is never the sole discriminator.
	if t.ExitCode == ExitCodeOOM && (t.Reason == "Error" || t.Reason == "") {
		signals["note"] = "classified from exit code after reason and message were uninformative"
		return Result{Type: OOMKilled, Rule: "terminated-exitcode-137-corroborated", Signals: signals}, true
	}

	// A nonzero exit is a failure worth reporting when the kubelet is backing
	// off, or when the container has restarted at least once and has not yet
	// been up long enough to count as recovered. The second case covers the
	// seconds a crash-looping container spends Running between restarts, which
	// the waiting reason does not describe.
	if t.ExitCode != 0 {
		if isCrashLoopWaiting(cs) {
			signals["state.waiting.reason"] = cs.State.Waiting.Reason
			return Result{Type: CrashLoopBackOff, Rule: "nonzero-exit-with-backoff", Signals: signals}, true
		}
		if cs.RestartCount > 0 {
			signals["restartCount"] = itoa(cs.RestartCount)
			return Result{Type: CrashLoopBackOff, Rule: "nonzero-exit-with-restarts", Signals: signals}, true
		}
	}

	return Result{}, false
}

func classifyWaitingCrashLoop(cs *corev1.ContainerStatus) (Result, bool) {
	if !isCrashLoopWaiting(cs) {
		return Result{}, false
	}
	return Result{
		Type: CrashLoopBackOff,
		Rule: "waiting-reason-is-crashloop-without-termination-detail",
		Signals: map[string]string{
			"state.waiting.reason": cs.State.Waiting.Reason,
			"restartCount":         itoa(cs.RestartCount),
		},
	}, true
}

func isCrashLoopWaiting(cs *corev1.ContainerStatus) bool {
	return cs.State.Waiting != nil && cs.State.Waiting.Reason == "CrashLoopBackOff"
}

func hasOOMMarker(msg string) bool {
	if msg == "" {
		return false
	}
	lower := strings.ToLower(msg)
	for _, m := range oomMarkers {
		if strings.Contains(lower, m) {
			return true
		}
	}
	return false
}

// Pod returns the first classified failure among a pod's containers, and the
// name of the container it came from.
func Pod(pod *corev1.Pod) (Result, string) {
	return PodAt(pod, time.Now())
}

// PodAt is Pod with an explicit clock.
func PodAt(pod *corev1.Pod, now time.Time) (Result, string) {
	if pod == nil {
		return Result{Type: None, Rule: "nil-pod"}, ""
	}
	for i := range pod.Status.InitContainerStatuses {
		cs := &pod.Status.InitContainerStatuses[i]
		if r := ContainerAt(cs, now); r.Type != None {
			return r, cs.Name
		}
	}
	for i := range pod.Status.ContainerStatuses {
		cs := &pod.Status.ContainerStatuses[i]
		if r := ContainerAt(cs, now); r.Type != None {
			return r, cs.Name
		}
	}
	return Result{Type: None, Rule: "no-container-failed"}, ""
}

func itoa(v int32) string {
	return strconv.FormatInt(int64(v), 10)
}
